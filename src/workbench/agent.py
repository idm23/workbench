"""Driving the Claude agent, and turning its output into storable events.

Two phases share one agent session. The plan phase runs read-only and stops
with a plan for a human to read; the execute phase resumes that same session
and carries it out. Resuming rather than re-prompting is what keeps the agent
from re-deriving everything it already worked out while planning.

Nothing here touches the database. The runner owns persistence; this module
turns SDK messages into plain dictionaries and yields them.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLINotFoundError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

logger = logging.getLogger(__name__)

#: A run that has not finished by now is stuck rather than slow.
MAX_TURNS_PLAN = 60
MAX_TURNS_EXECUTE = 200

#: System messages that carry no information for someone reading a run back.
#: `init` is setup chatter; `thinking_tokens` fires constantly and says only
#: that the model thought, which every other event already implies.
_UNINTERESTING_SYSTEM_SUBTYPES = frozenset({"init", "thinking_tokens"})


@dataclass(frozen=True)
class AgentEvent:
    """One thing that happened, ready to be persisted and streamed."""

    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class AgentFinished:
    """The run completed. `text` is the plan or the summary, as written."""

    text: str
    session_id: str | None
    total_cost_usd: float | None
    num_turns: int | None
    is_error: bool


@dataclass(frozen=True)
class AgentUnavailable:
    """The agent could not be started at all.

    Distinct from a run that started and failed: nothing was attempted, so
    there is nothing to summarise and the task is unchanged.
    """

    message: str


def plan_prompt(title: str, body: str | None) -> str:
    """The prompt for the planning phase.

    Deliberately does not ask for a specific format. Plan mode already
    produces a structured plan, and prescribing headings tends to yield a
    filled-in template rather than actual thinking about the task.
    """
    parts = [
        "You are working in a git worktree of this project, on a branch created "
        "for this task alone.",
        "",
        f"# Task: {title}",
    ]
    if body:
        parts += ["", body]
    parts += [
        "",
        "Investigate the codebase and produce a plan for this task. Do not make "
        "any changes yet — this is the planning phase, and a person will review "
        "your plan before anything is carried out.",
        "",
        "If the task is ambiguous, state the interpretation you are planning "
        "against rather than stopping to ask; there is no one to answer.",
    ]
    return "\n".join(parts)


def execute_prompt(title: str) -> str:
    """The prompt for the execute phase.

    Short because it resumes the planning session: the agent already has the
    task, the codebase context, and its own plan in the conversation.
    """
    return (
        "Your plan has been reviewed and approved. Carry it out now.\n"
        "\n"
        "Commit your work in logical commits as you go, with clear commit "
        "messages. Do not push, and do not open a pull request — Workbench does "
        f"both once you finish.\n"
        "\n"
        "When you are done, reply with a summary of what you changed and why, "
        "written for someone reviewing the pull request for "
        f"{title!r} without having watched you work. Mention anything you left "
        "undone or were unsure about."
    )


def _blocks_to_events(message: AssistantMessage) -> list[AgentEvent]:
    """Flatten an assistant message into one event per content block.

    Blocks with no text are skipped. Thinking arrives with its content
    redacted, which would otherwise fill the log with empty entries that push
    the real output off a phone screen.
    """
    events: list[AgentEvent] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            if block.text.strip():
                events.append(AgentEvent("text", {"text": block.text}))
        elif isinstance(block, ThinkingBlock):
            if block.thinking.strip():
                events.append(AgentEvent("thinking", {"text": block.thinking}))
        elif isinstance(block, ToolUseBlock):
            events.append(
                AgentEvent(
                    "tool_use",
                    {"name": block.name, "input": _summarise_tool_input(block.input)},
                )
            )
    return events


def _summarise_tool_input(raw: dict[str, Any]) -> dict[str, Any]:
    """Trim tool inputs to what is worth showing on a phone.

    A Write call carries an entire file; storing every one of them would make
    the event log larger than the diff it produced, for output nobody reads at
    that length.
    """
    trimmed: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, str) and len(value) > 400:
            trimmed[key] = f"{value[:400]}… ({len(value)} chars)"
        else:
            trimmed[key] = value
    return trimmed


def _tool_results(message: UserMessage) -> list[AgentEvent]:
    """Tool results, reduced to success or failure.

    The content itself is usually a file or command output the agent asked for
    and has already acted on. What matters when reading back a run is whether
    the step worked.
    """
    if not isinstance(message.content, list):
        return []

    events: list[AgentEvent] = []
    for block in message.content:
        if isinstance(block, ToolResultBlock):
            events.append(
                AgentEvent(
                    "tool_result",
                    {"is_error": bool(block.is_error), "preview": _preview(block.content)},
                )
            )
    return events


def _preview(content: Any) -> str:
    text = content if isinstance(content, str) else str(content)
    return text[:300]


async def run_agent(
    prompt: str,
    cwd: Path,
    permission_mode: str,
    max_turns: int,
    resume: str | None = None,
) -> AsyncIterator[AgentEvent | AgentFinished | AgentUnavailable]:
    """Run the agent, yielding events as they arrive and a terminator at the end.

    The final item is always AgentFinished or AgentUnavailable, so a consumer
    that stores everything it receives cannot end up with a run that has no
    recorded outcome.
    """
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        permission_mode=permission_mode,  # pyright: ignore[reportArgumentType]
        max_turns=max_turns,
        resume=resume,
        # The worktree is a checkout of the user's project, and its CLAUDE.md
        # and settings are exactly the context the agent should be working
        # under. `user` is excluded: the service account's global settings are
        # not part of the project.
        setting_sources=["project", "local"],
    )

    try:
        async for message in query(prompt=prompt, options=options):
            match message:
                case AssistantMessage():
                    for event in _blocks_to_events(message):
                        yield event
                case UserMessage():
                    for event in _tool_results(message):
                        yield event
                case SystemMessage():
                    if message.subtype not in _UNINTERESTING_SYSTEM_SUBTYPES:
                        yield AgentEvent("system", {"subtype": message.subtype})
                case ResultMessage():
                    yield AgentFinished(
                        text=message.result or "",
                        session_id=message.session_id,
                        total_cost_usd=message.total_cost_usd,
                        num_turns=message.num_turns,
                        is_error=message.is_error,
                    )
                case _:
                    continue
    except CLINotFoundError as error:
        yield AgentUnavailable(
            f"The bundled Claude CLI could not be found or started. Underlying error: {error}"
        )
    except Exception as error:
        # A bare except is the right call in exactly one place: this is the
        # boundary of a detached process, and an unrecorded crash here leaves a
        # run marked "running" forever with no explanation on screen.
        logger.exception("Agent run failed")
        yield AgentUnavailable(f"{type(error).__name__}: {error}")
