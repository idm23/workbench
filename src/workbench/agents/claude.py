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

import json
import logging
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import claude_agent_sdk
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
    CREDENTIAL_API_KEY,
    CREDENTIAL_NONE,
    CREDENTIAL_SUBSCRIPTION,
    CREDENTIAL_UNKNOWN,
    AgentEvent,
    AgentFailed,
    AgentFinished,
    AgentRequest,
    AgentStream,
    AgentUnavailable,
    CredentialStatus,
    SubtaskProposal,
)
from workbench.config import agent_environment, bills_subscription, port
from workbench.database.models import RunEventKind, RunPhase

logger = logging.getLogger(__name__)

BACKEND_NAME = "claude"

#: A run still going at this many turns is stuck rather than slow, and the
#: bound matters more than usual because nobody is watching it spend money.
MAX_TURNS_PLAN = 60
MAX_TURNS_EXECUTE = 200
#: A conversation is meant to run for a while, on and off, as someone keeps
#: typing into it — the idle timeout in `runs/runner.py` is what actually
#: ends it in the ordinary case, so this only needs to guard against one
#: that is somehow still being fed input turn after turn with nothing to
#: show for it.
MAX_TURNS_CONVERSATION = 500

#: Where the agent-facing skills live. Ships inside the repo rather than
#: being installed anywhere — `plugins` takes a plain path, so there is
#: nothing for `install.py` to do.
_PLUGIN_DIR = Path(__file__).parent / "plugin"
_OUTCOME_SKILL = "workbench-outcome"
_TASKS_SKILL = "workbench-tasks"

#: What a plan run's structured response must contain. Enforced by the SDK,
#: not parsed out of prose — `output_format` works under real plan mode
#: (`permission_mode="plan"`) because it shapes the final answer rather than
#: running a tool, which is the one thing that mode disallows outright.
_PLAN_OUTPUT_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "plan": {"type": "string"},
            "subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "ready_to_execute": {"type": "boolean"},
                    },
                    "required": ["title", "body", "ready_to_execute"],
                },
            },
        },
        "required": ["plan", "subtasks"],
    },
}

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
    if phase is RunPhase.PLAN:
        return MAX_TURNS_PLAN
    if phase is RunPhase.CONVERSATION:
        return MAX_TURNS_CONVERSATION
    return MAX_TURNS_EXECUTE


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


def _env_for(request: AgentRequest) -> dict[str, str]:
    """How a run finds its way back to Workbench's own API.

    Meaningful only for execute and conversation — the plan phase is held
    read-only and cannot call anything — but harmless to hand over either
    way.
    """
    return {
        "WORKBENCH_RUN_ID": str(request.run_id),
        "WORKBENCH_TASK_ID": str(request.task_id),
        "WORKBENCH_PROJECT_ID": str(request.project_id),
        "WORKBENCH_API_BASE": f"http://127.0.0.1:{port()}",
    }


def _options(request: AgentRequest) -> ClaudeAgentOptions:
    options: dict[str, Any] = {
        "cwd": request.worktree,
        "permission_mode": _permission_mode(request.phase),
        "max_turns": _max_turns(request.phase),
        "model": request.model,
        "resume": request.resume_token,
        # The agent works in a throwaway worktree of the project, so the
        # project's own CLAUDE.md and settings are exactly the context it
        # should have.
        "setting_sources": ["project"],
        "env": _env_for(request),
    }
    if request.phase is RunPhase.PLAN:
        # Structured output, not a tool call — the one decomposition
        # mechanism that works under real plan mode. See _PLAN_OUTPUT_FORMAT.
        options["output_format"] = _PLAN_OUTPUT_FORMAT
    elif request.phase is RunPhase.CONVERSATION:
        # Task management, not outcome reporting — a conversation has no
        # single task to call finished or failed.
        options["plugins"] = [{"type": "local", "path": str(_PLUGIN_DIR)}]
        options["skills"] = [_TASKS_SKILL]
    else:
        # The outcome-reporting skill only makes sense once tools can
        # actually run, which plan mode does not allow.
        options["plugins"] = [{"type": "local", "path": str(_PLUGIN_DIR)}]
        options["skills"] = [_OUTCOME_SKILL]
    return ClaudeAgentOptions(**options)


async def _prompt_stream(initial: str, inputs: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """The initial prompt, followed by whatever gets typed in later.

    `query()` accepts this shape lazily — confirmed live against the
    installed SDK, not just its docstring: fed one message up front, it sits
    waiting; a second one pushed onto the underlying queue minutes later is
    answered under the same session, and the loop only ends once this
    generator does. Closing `inputs` (the runner's idle timeout) is what
    lets a conversation actually finish rather than run forever.
    """
    yield {"type": "user", "message": {"role": "user", "content": initial}}
    async for text in inputs:
        yield {"type": "user", "message": {"role": "user", "content": text}}


def _prompt_for(request: AgentRequest) -> str | AsyncIterator[dict[str, Any]]:
    """A plain string when nothing can type into this run, exactly as
    before; the lazy shape only once something actually might."""
    if request.inputs is None:
        return request.prompt
    return _prompt_stream(request.prompt, request.inputs)


def _stopped_early(result: ResultMessage) -> bool:
    """Whether the CLI cut the turn short itself, rather than the agent
    choosing to stop. Distinct from an outright error: this is still a
    `ResultMessage` that reached us, just not one to trust a self-reported
    "finished" against."""
    reason = getattr(result, "terminal_reason", None)
    return reason is not None and reason != "completed"


def _plan_text(result: ResultMessage) -> str:
    """The plan's prose, from structured output when there is any.

    `result.result` is the schema's JSON serialised as text when
    `output_format` was set — showing that to a person as "the plan" would
    be unreadable, so the `plan` field is what actually gets shown.
    """
    structured = result.structured_output
    if isinstance(structured, dict) and isinstance(structured.get("plan"), str):
        return structured["plan"]
    return result.result or ""


def _proposed_subtasks(result: ResultMessage) -> list[SubtaskProposal] | None:
    """A plan's decomposition, from the same structured output.

    Defensive about shape despite the schema: a model can still deviate, and
    this is the one place that would ever see it.
    """
    structured = result.structured_output
    if not isinstance(structured, dict):
        return None
    raw = structured.get("subtasks")
    if not isinstance(raw, list):
        return None
    return [
        SubtaskProposal(
            title=str(item["title"]),
            body=str(item.get("body") or ""),
            ready_to_execute=bool(item.get("ready_to_execute", False)),
        )
        for item in raw
        if isinstance(item, dict) and item.get("title")
    ]


#: How long to wait for the credential probe. It is an offline read of a file
#: in a home directory — measured at well under a second — so anything near
#: this bound means the CLI is wedged, and a health check must not hang on it.
AUTH_PROBE_TIMEOUT_SECONDS = 20


def _cli_path() -> str | None:
    """Where the CLI this backend would actually run lives.

    The SDK bundles a binary and prefers it over anything on `PATH`, so asking
    `PATH` first would happily report a different Claude to the one a run uses
    — which is the one way this check could be worse than no check. Derived
    from the package's own location rather than by constructing a transport,
    because building one needs a live connection's worth of arguments.

    This is the only SDK-internal knowledge in the file, and it is here for the
    same reason everything else vendor-shaped is: nothing above the seam may
    know that a bundled binary exists.
    """
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("claude")


def _unknown_credential(detail: str) -> CredentialStatus:
    """A probe that could not be made. Deliberately not a failure — see the
    note on `CREDENTIAL_UNKNOWN` in `protocol.py`."""
    return CredentialStatus(
        backend=BACKEND_NAME,
        logged_in=False,
        method=CREDENTIAL_UNKNOWN,
        detail=detail,
    )


def _login_command(cli: str | None) -> tuple[str, ...]:
    """How a person signs this backend in.

    `--claudeai` is spelled out rather than left to the default: the other
    branch is `--console`, which authenticates perfectly and bills the metered
    API, and an instruction that lets someone pick it by accident is the same
    silent failure this whole check exists to catch.
    """
    return () if cli is None else (cli, "auth", "login", "--claudeai")


def read_credential(payload: dict[str, Any], cli: str | None = None) -> CredentialStatus:
    """Translate the CLI's `auth status` report into Workbench's vocabulary.

    Split out from the subprocess call so the mapping can be tested against
    real recorded payloads without a binary, which matters because the
    interesting cases here are the ones that are awkward to reproduce on
    demand.
    """
    method = str(payload.get("authMethod") or "").strip()
    account = payload.get("email") or payload.get("orgName") or None

    if method == "claude.ai":
        who = account or "this account"
        return CredentialStatus(
            backend=BACKEND_NAME,
            logged_in=True,
            method=CREDENTIAL_SUBSCRIPTION,
            account=account,
            detail=f"Signed in as {who}, billing a Claude subscription.",
            login_command=_login_command(cli),
        )

    if method == "api_key":
        source = str(payload.get("apiKeySource") or "an API key")
        if bills_subscription():
            # Reachable despite `agent_environment()` having stripped the
            # variables it knows about: a key can also arrive from a settings
            # file or an `apiKeyHelper`, which no amount of environment
            # scrubbing reaches. That is precisely the case worth shouting
            # about, because nothing visible in Workbench would change.
            return CredentialStatus(
                backend=BACKEND_NAME,
                logged_in=False,
                method=CREDENTIAL_API_KEY,
                account=account,
                detail=(
                    f"Authenticated by {source}, which bills the metered API rather than "
                    "the subscription this instance is configured for. Remove it, or set "
                    "WORKBENCH_BILLING=api to choose metered billing on purpose."
                ),
                login_command=_login_command(cli),
            )
        return CredentialStatus(
            backend=BACKEND_NAME,
            logged_in=True,
            method=CREDENTIAL_API_KEY,
            account=account,
            detail=f"Billing the metered API, authenticated by {source}.",
        )

    if not payload.get("loggedIn"):
        return CredentialStatus(
            backend=BACKEND_NAME,
            logged_in=False,
            method=CREDENTIAL_NONE,
            detail="Not signed in — no agent can run as this account yet.",
            login_command=_login_command(cli),
        )

    return _unknown_credential(
        f"The CLI reported an authentication method Workbench does not know: {method!r}."
    )


class ClaudeBackend:
    """Drives Claude through `claude_agent_sdk`.

    Holds no state between runs: a run is one call to `run()`, and continuity
    across phases comes from the resume token, not from this object.
    """

    @property
    def name(self) -> str:
        return BACKEND_NAME

    def credential_status(self) -> CredentialStatus:
        """Ask the CLI who it would authenticate as. See the protocol's note.

        Run under `agent_environment()`, which is the whole point: it strips
        the metered-API variables exactly as the runner does, so this reports
        what a run would get rather than what happens to be exported in the
        shell that asked.

        The exit code is ignored on purpose — a logged-out CLI answers 1 and
        still prints a perfectly good report on stdout, so treating non-zero
        as unreadable would turn the most important case into `unknown`.
        """
        cli = _cli_path()
        if cli is None:
            return _unknown_credential(
                "The Claude CLI could not be found, so the credential was not checked."
            )

        try:
            probe = subprocess.run(
                [cli, "auth", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=AUTH_PROBE_TIMEOUT_SECONDS,
                env=agent_environment(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _unknown_credential(
                f"The Claude CLI did not answer within {AUTH_PROBE_TIMEOUT_SECONDS}s."
            )
        except OSError as exc:
            return _unknown_credential(f"The Claude CLI could not be run: {exc}.")

        try:
            payload = json.loads(probe.stdout)
        except json.JSONDecodeError:
            logger.warning("Unreadable auth status from %s: %r", cli, probe.stdout[:200])
            return _unknown_credential("The Claude CLI did not report a readable status.")

        if not isinstance(payload, dict):
            return _unknown_credential("The Claude CLI did not report a readable status.")
        return read_credential(payload, cli)

    async def run(self, request: AgentRequest) -> AgentStream:
        model: str | None = None
        result: ResultMessage | None = None

        try:
            async for message in query(prompt=_prompt_for(request), options=_options(request)):
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

        is_plan = request.phase is RunPhase.PLAN
        finished = AgentFinished(
            text=_plan_text(result) if is_plan else (result.result or ""),
            resume_token=result.session_id,
            model=model,
            total_cost_usd=result.total_cost_usd,
            num_turns=result.num_turns,
            proposed_subtasks=_proposed_subtasks(result) if is_plan else None,
            stopped_early=_stopped_early(result),
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
