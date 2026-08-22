"""The Claude backend: the only module in Workbench that imports an agent SDK.

Everything vendor-specific is here — the SDK import, its message classes, its
option names, its notion of a session. The rest of the codebase sees an
`AgentRequest` going in and `AgentEvent`s coming out, which is what makes a
second backend a new file rather than a refactor. `tests/test_seam.py` fails if
this import appears anywhere else.

Two things this module owes the rest of the app. It translates the SDK's
messages into `RunEventKind` rather than passing them through, so the event log
outlives the SDK that wrote it. And it never raises: a missing CLI or a crashed
subprocess comes back as an outcome, because those are ordinary conditions on a
home server and a traceback out of a detached runner helps nobody.
"""

import logging
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    PermissionMode,
    RateLimitEvent,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from workbench.agents.protocol import (
    AgentEvent,
    AgentFailed,
    AgentFinished,
    AgentRequest,
    AgentStream,
    AgentUnavailable,
)
from workbench.database.models import RunEventKind, RunPhase

logger = logging.getLogger(__name__)

BACKEND_NAME = "claude"

#: A run still going at this many turns is stuck rather than slow, and the
#: bound matters more than usual because nobody is watching it spend money.
MAX_TURNS_PLAN = 60
MAX_TURNS_EXECUTE = 200

#: Longest string kept in an event payload. A single Write tool call can carry
#: an entire file, and every event is a row that is kept forever — truncating
#: at the point of translation is the cheapest place to bound the table. The
#: full content is on disk in the worktree either way.
MAX_PAYLOAD_CHARS = 10_000

#: System messages that tell a reader nothing. `init` is setup chatter, and
#: `thinking_tokens` fires constantly to report only that the model thought,
#: which the thinking events already say.
_UNINTERESTING_SYSTEM_SUBTYPES = frozenset({"init", "thinking_tokens"})


def _permission_mode(phase: RunPhase) -> PermissionMode:
    """How much the agent is allowed to do, by phase.

    `acceptEdits` is the trap here, and it cost 33 turns to find: it permits
    file edits but still gates Bash, so the agent edits happily and then cannot
    run `git commit`. It retries, fails the same way, and burns the turn limit
    achieving nothing — and because a run is detached there is no prompt for
    anyone to answer.

    So the execute phase runs with permissions bypassed. That is not as
    alarming as it reads, but only because of where the real boundary is: an
    agent is bounded by the account it runs as, not by the permission mode it
    was started with. A mode that gates Bash on a headless run does not contain
    the agent, it just makes it fail. The containment is the unprivileged
    service user, no sudo, and a working tree whose contents are recoverable
    from GitHub.

    The plan phase is different, and gets the SDK's real plan mode: read-only
    is the *product* there, not a restriction on it, so enforcement and
    intention agree.
    """
    return "plan" if phase is RunPhase.PLAN else "bypassPermissions"


def _max_turns(phase: RunPhase) -> int:
    return MAX_TURNS_PLAN if phase is RunPhase.PLAN else MAX_TURNS_EXECUTE


def _clip(text: str) -> str:
    """Bound a payload string, saying so when it is cut."""
    if len(text) <= MAX_PAYLOAD_CHARS:
        return text
    dropped = len(text) - MAX_PAYLOAD_CHARS
    return f"{text[:MAX_PAYLOAD_CHARS]}\n… truncated, {dropped} more characters"


def _clip_value(value: Any) -> Any:
    """Bound the strings inside a tool's input, keeping its shape.

    Tool inputs are dictionaries whose values are mostly small — a path, a
    pattern — with one occasionally enormous one. Clipping per value keeps the
    small keys readable instead of truncating the whole JSON blob mid-object.
    """
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, dict):
        return {key: _clip_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_clip_value(inner) for inner in value]
    return value


def _tool_result_text(content: str | list[dict[str, Any]] | None) -> str:
    """Flatten a tool result into something a browser can render.

    The SDK hands back either a string or a list of content blocks. The blocks
    that are not text — images, mostly — are named rather than dropped, so a
    reader can tell that something came back and it was not prose.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return _clip(content)

    parts: list[str] = []
    for block in content:
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            parts.append(f"[{block.get('type', 'unknown')}]")
    return _clip("\n".join(parts))


def _blocks_to_events(content: list[Any]) -> list[AgentEvent]:
    """Translate one message's content blocks into Workbench's vocabulary.

    A block type with no equivalent becomes a `notice` rather than a new
    `RunEventKind` — the vocabulary grows when a *reader* needs the
    distinction, not whenever an SDK adds a class.
    """
    events: list[AgentEvent] = []
    for block in content:
        match block:
            case TextBlock():
                if block.text.strip():
                    events.append(AgentEvent(RunEventKind.TEXT, {"text": _clip(block.text)}))
            case ThinkingBlock():
                if block.thinking.strip():
                    events.append(
                        AgentEvent(RunEventKind.THINKING, {"text": _clip(block.thinking)})
                    )
            case ToolUseBlock() | ServerToolUseBlock():
                events.append(
                    AgentEvent(
                        RunEventKind.TOOL_USE,
                        {
                            "id": block.id,
                            "name": block.name,
                            "input": _clip_value(block.input),
                        },
                    )
                )
            case ToolResultBlock() | ServerToolResultBlock():
                events.append(
                    AgentEvent(
                        RunEventKind.TOOL_RESULT,
                        {
                            "id": getattr(block, "tool_use_id", None),
                            "text": _tool_result_text(getattr(block, "content", None)),
                            "is_error": bool(getattr(block, "is_error", False)),
                        },
                    )
                )
            case _:
                events.append(
                    AgentEvent(
                        RunEventKind.NOTICE,
                        {"text": f"Unrecognised block: {type(block).__name__}"},
                    )
                )
    return events


def translate(message: Any) -> list[AgentEvent]:
    """One SDK message to zero or more events.

    Split out from the streaming loop so the mapping can be tested against
    constructed messages, with no subprocess, no credential, and no model.
    """
    match message:
        case AssistantMessage():
            events = _blocks_to_events(message.content)
            if message.error:
                events.append(
                    AgentEvent(RunEventKind.NOTICE, {"text": f"Backend error: {message.error}"})
                )
            return events

        case UserMessage():
            # A string here is Workbench's own prompt echoed back; the list
            # form is how tool results arrive.
            if isinstance(message.content, str):
                return []
            return _blocks_to_events(message.content)

        case SystemMessage():
            if message.subtype in _UNINTERESTING_SYSTEM_SUBTYPES:
                return []
            return [
                AgentEvent(
                    RunEventKind.NOTICE,
                    {"text": f"System: {message.subtype}", "data": _clip_value(message.data)},
                )
            ]

        case RateLimitEvent():
            # The signal that actually matters when runs bill a subscription.
            # Money is not the scarce resource there — the five-hour and weekly
            # windows are — and this is the only place the backend says how
            # much of one is left. Recorded structured rather than as prose so
            # a reader can find the run that exhausted a window without
            # rereading every notice.
            info = message.rate_limit_info
            return [
                AgentEvent(
                    RunEventKind.NOTICE,
                    {
                        "text": f"Rate limit {info.status} ({info.rate_limit_type}).",
                        "rate_limit": {
                            "status": info.status,
                            "type": info.rate_limit_type,
                            "utilization": info.utilization,
                            "resets_at": info.resets_at,
                        },
                    },
                )
            ]

        case ResultMessage():
            # Becomes the outcome, not an event.
            return []

        case _:
            return [
                AgentEvent(
                    RunEventKind.NOTICE,
                    {"text": f"Unrecognised message: {type(message).__name__}"},
                )
            ]


def _options(request: AgentRequest) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cwd=request.worktree,
        permission_mode=_permission_mode(request.phase),
        max_turns=_max_turns(request.phase),
        model=request.model,
        resume=request.resume_token,
        # The agent works in a throwaway worktree of the project, so the
        # project's own CLAUDE.md and settings are exactly the context it
        # should have.
        setting_sources=["project"],
    )


class ClaudeBackend:
    """Drives Claude through `claude_agent_sdk`.

    Holds no state between runs: a run is one call to `run()`, and continuity
    across phases comes from the resume token, not from this object.
    """

    @property
    def name(self) -> str:
        return BACKEND_NAME

    async def run(self, request: AgentRequest) -> AgentStream:
        model: str | None = None
        result: ResultMessage | None = None

        try:
            async for message in query(prompt=request.prompt, options=_options(request)):
                if isinstance(message, AssistantMessage) and message.model:
                    # What actually answered, which is what gets recorded —
                    # `request.model` is only ever a preference.
                    model = message.model
                if isinstance(message, ResultMessage):
                    result = message
                for event in translate(message):
                    yield event

        except CLINotFoundError as exc:
            # Nothing was attempted: the wheel's bundled CLI is missing or
            # unrunnable, which is an install problem rather than a run that
            # went wrong.
            logger.exception("Claude CLI unavailable.")
            yield AgentUnavailable(f"The Claude CLI could not be started: {exc}")
            return

        except ClaudeSDKError as exc:
            # Everything else the SDK raises — a dropped connection, a crashed
            # subprocess, undecodable output. The agent may well have committed
            # work before this, so it is a failure rather than unavailability.
            logger.exception("Claude run failed.")
            yield AgentFailed(f"The agent failed: {exc}", model=model)
            return

        if result is None:
            # The stream ended without the SDK's final message. Whatever the
            # agent did is still in the worktree, so this is a failed run and
            # not an unavailable one.
            yield AgentFailed("The agent stopped without reporting a result.", model=model)
            return

        finished = AgentFinished(
            text=result.result or "",
            resume_token=result.session_id,
            model=model,
            total_cost_usd=result.total_cost_usd,
            num_turns=result.num_turns,
        )
        if result.is_error:
            yield AgentFailed(
                message=result.result or f"The agent reported an error ({result.subtype}).",
                resume_token=finished.resume_token,
                model=finished.model,
                total_cost_usd=finished.total_cost_usd,
                num_turns=finished.num_turns,
            )
            return
        yield finished
