"""The local backend: an agent loop over an OpenAI-compatible endpoint.

The second backend, and the first evidence that the seam CLAUDE.md describes
actually holds. It imports no vendor SDK at all — `/chat/completions` is spoken
identically by Ollama, `llama-server` and vLLM, so the choice between them is a
URL rather than a code path, and `tests/test_seam.py` constrains this file
exactly as it constrains everything above it rather than exempting it.

What is different here is that there is no agent on the other end, only a
model. Workbench supplies the tools (`workbench.agents.tools`), drives the
turn loop, decides when the run is over, and keeps the transcript. Three
consequences worth knowing before reading on:

- **The plan phase is read-only by construction**, because the tools that
  write are not in the list sent for it.
- **The transcript is ours**, written under `data/sessions/` and named by an
  opaque token. A local endpoint keeps no session, so this backend has to —
  which incidentally means deleting a worktree does not orphan a conversation
  the way a directory-scoped session would.
- **Nothing here bills anything.** `total_cost_usd` stays null: the run spends
  a GPU, and inventing a dollar figure for it would put a number in a column
  that is read as money.
"""

import json
import logging
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from workbench.agents.protocol import (
    CREDENTIAL_LOCAL,
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
from workbench.agents.tools import (
    OUTCOME_TOOLS,
    PlanSubmitted,
    ToolContext,
    ToolResult,
    clip,
    dispatch,
    nothing_happened,
    tool_names_for,
    tools_for,
    uncommitted_work,
)
from workbench.config import (
    inference_base_url,
    inference_context_tokens,
    inference_timeout_seconds,
    local_model,
    port,
    sessions_dir,
)
from workbench.database.models import RunEventKind, RunPhase

logger = logging.getLogger(__name__)

BACKEND_NAME = "local"

#: Turn caps, lower than the Claude adapter's for a reason that is not
#: caution: a turn on an 8 GB card can take a minute, so a limit of 200 is not
#: a limit at all — the run's hour-long systemd timeout would arrive first and
#: kill it with nothing recorded. These are chosen to be reached *before* that.
MAX_TURNS_PLAN = 40
MAX_TURNS_EXECUTE = 120
MAX_TURNS_CONVERSATION = 300

#: How many times a run that has done nothing is asked to carry on before it
#: is allowed to end. Bounded rather than absent: a model that will not act is
#: a model that will not act, and an unbounded nudge is a rate limit spent on
#: asking the same question.
MAX_NUDGES = 2

#: What that push says. Deliberately concrete — "continue" alone gets another
#: paragraph of intent, where naming the next physical act gets a tool call.
_NUDGE = (
    "You have not changed anything yet, so the task is not done. Do not describe "
    "what you will do — do it now, with one tool call, starting from what the "
    "last result actually said."
)

#: What an execute run is told when it stops with work uncommitted.
_FINISH_NUDGE = (
    "You have changes in the worktree that are not committed, and you have not "
    "called report_outcome. Check your work (run the tests if there are any), "
    "commit it with run_command, then call report_outcome."
)

#: A plan run that stops with neither a tool call nor any text has not
#: finished, it has stalled — there is nothing to call a plan. Before this
#: existed, one such turn failed the run outright, and a plan run was the only
#: phase with no nudge at all.
_PLAN_NUDGE = (
    "You have not submitted a plan yet. Continue with a tool call, or call "
    "submit_plan now if you have read enough to decide."
)

#: When a run is told how many turns it has left, as a fraction of its cap.
#:
#: Nothing told a model its budget, and a capable one does not stop reading on
#: its own. gpt-oss, given its whole task and its own reasoning back, read the
#: right files — `app.py`, `activity.py`, `lifecycle.py`, the template, the
#: route tests — and then kept going until the cap ended the run with no plan,
#: twice in a row. A person told "twenty left" wraps up; so, it turns out, does
#: the model.
BUDGET_NOTICE_FRACTION = 0.5


def _tell(messages: list[dict[str, Any]], note: str) -> None:
    """Put a note from the loop in front of the model without ending its chain.

    Appended to the latest tool result when there is one, rather than sent as
    a user message, and that is the whole point of this function. Chat
    templates drop reasoning from before the most recent user message — the
    same rule that makes carrying reasoning forward work at all — so a note
    delivered as a user turn mid-run would silently wipe everything the model
    had thought so far, in exchange for telling it the time.
    """
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        last["content"] = f"{last.get('content') or ''}\n\n[Workbench] {note}"
    else:
        messages.append({"role": "user", "content": note})


#: The first user turn of a fresh session, once the task lives in the system
#: message. Something has to be there — chat templates expect a user turn —
#: and it is deliberately worth nothing, so losing it to truncation loses
#: nothing.
_BEGIN = "Begin the task described in your instructions."


#: How much of the model's window a resumed conversation may already fill.
#: Past this there is no room left to work: run 73 resumed a session that was
#: at the 32k limit on its first turn, and floundered until it gave up.
RESUME_BUDGET_FRACTION = 0.5

#: Said to a model starting fresh where an earlier attempt left off, because
#: the worktree is not fresh even though the conversation is.
_AFTER_A_LONG_ATTEMPT = (
    "An earlier attempt at this task ran in this worktree, and its conversation was "
    "too long to continue. Before changing anything, run `git status` and "
    "`git log --oneline -5` to see what it left, and build on that rather than "
    "starting over."
)


def _estimated_tokens(messages: list[dict[str, Any]]) -> int:
    """A rough size for a stored conversation: about four characters a token.

    Rough on purpose. The question is only whether a transcript leaves room to
    work, and the answer for the ones that caused trouble was not close.
    """
    chars = 0
    for message in messages:
        chars += len(message.get("content") or "") + len(message.get("reasoning") or "")
        chars += sum(len(json.dumps(call)) for call in message.get("tool_calls") or [])
    return chars // 4


def _resume_budget() -> int:
    """How big a stored conversation may be and still be continued.

    Read from the same setting that sizes the node's window
    (`WORKBENCH_INFERENCE_CONTEXT_TOKENS`); a head whose node uses a different
    window should set it to match.
    """
    return int(inference_context_tokens() * RESUME_BUDGET_FRACTION)


def _pinned(system: str, task: str) -> str:
    """The system message with the task inside it, where truncation cannot reach.

    A model server whose window is too small does not refuse: Ollama drops the
    oldest messages until the rest fit, and it keeps the system messages while
    doing it. The first user message is the oldest thing it is allowed to drop
    — and that is where the task used to be. So the one message guaranteed to
    survive held the rules, and the one guaranteed to go first held the job:
    run 63 read the right files for nine turns and then asked whether there
    had been a user query yet.

    With the task here, a run that outgrows its window loses the oldest tool
    output instead of its instructions, which is a degradation rather than a
    different task. It does not replace a window big enough for the run — see
    `install_node._drop_in` — or the notice when truncation happens anyway.
    """
    return f"{system}\n\n---\n\n{task}"


def _budget_note(phase: RunPhase, remaining: int) -> str | None:
    if phase is RunPhase.PLAN:
        return (
            f"{remaining} turns remain in this planning run. Read only what you "
            "still need, then call submit_plan."
        )
    if phase is RunPhase.EXECUTE:
        return (
            f"{remaining} turns remain in this run. Make sure your work is "
            "committed and call report_outcome before they run out."
        )
    # A conversation is paced by a person, and its cap is a backstop rather
    # than a budget anyone should be working to.
    return None


#: The last turn of a plan run offers exactly one tool. A model given a single
#: option with an instruction to use it does, where "you should submit soon"
#: among eight tools is a suggestion — and a plan written from forty turns of
#: reading is worth far more than the "produced no plan" that replaced it.
_FINAL_PLAN_TURN = (
    "This is the last turn of the planning run. Call submit_plan now, with the "
    "best plan you can make from what you have already read."
)


#: How Ollama words the 500 it returns for a tool call it could not parse.
_UNPARSED_TOOL_CALL = "error parsing tool call"

_RESEND_TOOL_CALL = (
    "Your last tool call was cut off or was not valid JSON, so it did not run. "
    "Send it again, complete, with arguments as a JSON object."
)

#: How many malformed tool calls in a row before this is not going to work.
#: Small models emit unparseable arguments; they usually recover when told,
#: and when they do not they do it forever.
MAX_CONSECUTIVE_TOOL_FAILURES = 4

#: Text is buffered into events rather than emitted per delta: every event is
#: a row, and a row per token would be tens of thousands of them for one run.
#: Flushed on either bound so a slow model still shows something moving.
TEXT_FLUSH_CHARS = 400
TEXT_FLUSH_SECONDS = 2.0

#: Thinking is buffered far harder, and that is not a tidiness preference. A
#: reasoning model talks to itself at length — one qwen3 run here produced over
#: a hundred rows of it while making three tool calls — and those rows are kept
#: forever, scrolled past by a person looking for what the agent actually did.
#: A reader needs to see that it is thinking and roughly about what; they do not
#: need the transcript at the granularity of the answer.
THINKING_FLUSH_CHARS = 2_000
THINKING_FLUSH_SECONDS = 15.0

#: How long the credential probe waits. It runs on a page render path, so it
#: has to fail fast when nothing is listening.
PROBE_TIMEOUT_SECONDS = 5.0


#: How a model writes a tool call when it does not use the tool-call channel.
#: Qwen wraps them in these; Ollama's parser only recognises the tagged form,
#: so an untagged one arrives as ordinary prose and this is what finds it.
_TOOL_CALL_TAGS = ("<tool_call>", "</tool_call>", "```json", "```")


def _tool_calls_from_text(text: str, allowed: set[str]) -> list[dict[str, Any]]:
    """Tool calls a model wrote out as text instead of calling.

    Small models do this constantly, and the first real run against one did it
    on its first turn: a perfectly well-formed `report_outcome` call, in the
    message content, which the loop then read as "no tool calls, so it must be
    finished" and recorded as a summary. The reply was JSON; the run said
    succeeded.

    Recovering is cheap and the guard against over-recovering is `allowed`: the
    object has to name a tool that actually exists in this phase. A summary
    that happens to contain a JSON example is therefore still a summary, which
    is the case that would otherwise turn a finished run into an endless one.
    """
    if not text or "{" not in text:
        return []

    stripped = text
    for tag in _TOOL_CALL_TAGS:
        stripped = stripped.replace(tag, " ")

    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    index = 0
    while (start := stripped.find("{", index)) != -1:
        try:
            value, end = decoder.raw_decode(stripped, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        index = end
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        if not isinstance(name, str) or name not in allowed:
            continue
        # `parameters` is the other spelling in the wild; both mean the same.
        arguments = value.get("arguments")
        if arguments is None:
            arguments = value.get("parameters")
        found.append(
            {
                "id": f"recovered_{len(found)}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments
                    if isinstance(arguments, str)
                    else json.dumps(arguments if isinstance(arguments, dict) else {}),
                },
            }
        )
    return found


def _head_of(worktree: Path) -> str | None:
    """The worktree's current commit, so a run can be asked afterwards whether
    it did anything. None when it cannot be read — an unborn branch, a
    directory that is not a checkout — which reads as "cannot say"."""
    try:
        found = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return found.stdout.strip() if found.returncode == 0 else None


def _client(base_url: str | None = None) -> httpx.AsyncClient:
    """The connection to the model server.

    A function rather than an inline constructor so a test can hand back one
    wired to a transport instead of a socket — the same reason the Claude
    adapter keeps `_cli_path()` separate from the code that uses it.
    """
    return httpx.AsyncClient(
        base_url=base_url or inference_base_url(), timeout=inference_timeout_seconds()
    )


def _probe() -> httpx.Client:
    """The short-lived connection the credential check uses. Separate from
    `_client` because it is synchronous and must fail fast: it runs while a
    page is rendering."""
    return httpx.Client(base_url=inference_base_url(), timeout=PROBE_TIMEOUT_SECONDS)


def _max_turns(phase: RunPhase) -> int:
    if phase is RunPhase.PLAN:
        return MAX_TURNS_PLAN
    if phase is RunPhase.CONVERSATION:
        return MAX_TURNS_CONVERSATION
    return MAX_TURNS_EXECUTE


def system_prompt(phase: RunPhase) -> str:
    """What the model is, and how this loop expects to be talked to.

    Deliberately not where the *task* is described — that is
    `workbench.agents.prompts`, which is backend-independent. This only covers
    what a model behind a bare endpoint cannot know: that it has tools, that
    nobody is watching, and how the run ends.
    """
    common = (
        "You are a software engineer working autonomously inside a git worktree. "
        "You have tools; use them. Never claim to have read, changed, or run "
        "something without actually calling the tool that does it, and never "
        "write a tool call out as text in your reply — call it.\n"
        "\n"
        "Always look before you conclude. Read the files the task names, and the "
        "ones around them, before deciding anything — including deciding that the "
        "task cannot be done. A verdict reached without reading anything is a "
        "guess, and this run has no one to correct it.\n"
        "\n"
        "Nobody is watching this session as it runs. Work in small steps, and "
        "check what you changed afterwards."
    )
    if phase is RunPhase.PLAN:
        return (
            f"{common}\n"
            "\n"
            "This is a planning run and you have read-only tools only, so there "
            "is no way to ask anything from here: decide, and say which "
            "interpretation you chose. When your investigation is done, call "
            "submit_plan exactly once with the plan in prose. That ends the run."
        )
    if phase is RunPhase.CONVERSATION:
        return (
            f"{common}\n"
            "\n"
            "This is a conversation. Answer what is asked, then stop and wait for "
            "the next message rather than inventing more work."
        )
    return (
        f"{common}\n"
        "\n"
        "Work in this order: read what the task refers to, make the change, check "
        "it, commit it with run_command, then call report_outcome once. Do not "
        "push and do not open a pull request — Workbench does both once you "
        "finish. After report_outcome, reply with your summary and no further "
        "tool calls: that reply ends the run and is what a reviewer reads.\n"
        "\n"
        "If you hit a fork you genuinely cannot settle, and the answer changes "
        "what gets built, call ask_user instead of guessing and stop there. Not "
        "for a detail you could choose and mention — every question costs a "
        "person their attention, and asking about everything is worse than "
        "deciding and saying so."
    )


@dataclass
class _Assistant:
    """One assembled reply, whatever order the deltas arrived in."""

    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    #: How many tokens the server says it actually read, when it says. What
    #: `_Window` compares against what was sent.
    prompt_tokens: int | None = None

    def as_message(self) -> dict[str, Any]:
        """The reply as it goes back into the conversation — reasoning included.

        Leaving the reasoning out was the quieter of the two things that made
        a reasoning model lose its own task. A model that thinks, calls a tool,
        and is then shown the result *without* the thought that asked for it
        has to reconstruct its intent from tool output alone, every turn.

        `reasoning` is the spelling that matters, and that was measured rather
        than guessed. With a fact placed only in the prior turn's reasoning,
        inside a tool-call chain, both `gpt-oss:20b` and `qwen3:8b` on Ollama's
        `/v1` recalled it under `reasoning` and neither did under
        `reasoning_content` or `thinking`. Reading accepts all three spellings
        (`_apply_delta`); writing needs only the one a server will read back.

        Sent whenever there is any, and the server decides what to keep. The
        chat templates drop reasoning from before the latest user message and
        keep it within a tool-call chain — which is exactly gpt-oss's own rule,
        and why the same probe placed *before* a user message recalled nothing.
        """
        message: dict[str, Any] = {"role": "assistant", "content": self.text}
        if self.reasoning:
            message["reasoning"] = self.reasoning
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return message


def _merge_tool_call_delta(calls: list[dict[str, Any]], delta: dict[str, Any]) -> None:
    """Fold one streamed tool-call fragment into what has arrived so far.

    Streaming splits a single call across many chunks — the name in one, the
    arguments a few characters at a time — keyed by `index`. Assembling by
    index rather than by arrival order is the whole of it, and getting it
    wrong produces a call whose arguments are the concatenation of two.
    """
    index = int(delta.get("index") or 0)
    while len(calls) <= index:
        calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    call = calls[index]
    if delta.get("id"):
        call["id"] = delta["id"]
    fragment = delta.get("function") or {}
    if fragment.get("name"):
        call["function"]["name"] = fragment["name"]
    if fragment.get("arguments"):
        call["function"]["arguments"] += fragment["arguments"]


def _apply_delta(delta: dict[str, Any], reply: _Assistant) -> tuple[str, str]:
    """Add one delta to the reply, returning the new (text, reasoning) fragments.

    Three spellings of the same field, because there is no standard: Ollama
    sends `thinking`, vLLM and friends send `reasoning_content`, some send
    `reasoning`. Accepting all three is cheaper than a per-server branch.
    """
    text = delta.get("content") or ""
    reasoning = (
        delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking") or ""
    )
    reply.text += text
    reply.reasoning += reasoning
    for fragment in delta.get("tool_calls") or []:
        _merge_tool_call_delta(reply.tool_calls, fragment)
    return text, reasoning


class _Buffer:
    """Coalesces streamed text into events worth storing as rows.

    Two instances with different bounds, because the two kinds are read
    differently: a person follows the reply as it arrives, and skims the
    reasoning to see whether it is going anywhere.
    """

    def __init__(self, kind: RunEventKind, chars: int, seconds: float) -> None:
        self._kind = kind
        self._chars = chars
        self._seconds = seconds
        self._text = ""
        self._since = datetime.now(UTC)

    def add(self, fragment: str) -> AgentEvent | None:
        self._text += fragment
        elapsed = (datetime.now(UTC) - self._since).total_seconds()
        if len(self._text) >= self._chars or elapsed >= self._seconds:
            return self.flush()
        return None

    def flush(self) -> AgentEvent | None:
        text, self._text = self._text, ""
        self._since = datetime.now(UTC)
        if not text.strip():
            return None
        return AgentEvent(self._kind, {"text": clip(text)})


#: Characters per token, as a floor on how much a message *must* cost. Real
#: text runs three to five; eight is loose enough that nothing honest trips it,
#: which matters more here than catching every case.
_CHARS_PER_TOKEN_CEILING = 8

#: How far short of that floor the server's count must fall before it is
#: called truncation. Truncation drops whole messages — the one this watcher
#: caught for real was 1,200 tokens short — while the floor itself can be a few
#: tokens high on text that tokenises unusually well: a wall of pytest's dots
#: set it off at 16,754 against 16,756, with the window half empty.
_TRUNCATION_MARGIN_TOKENS = 256


def _chars(message: dict[str, Any]) -> int:
    calls = message.get("tool_calls") or []
    return (
        len(message.get("content") or "")
        + len(message.get("reasoning") or "")
        + sum(len(json.dumps(call.get("function") or {})) for call in calls)
    )


@dataclass
class _Window:
    """Notices when the model server has started dropping the conversation.

    Truncation is silent by design on the server's side, and that silence is
    what made it cost three runs before anyone looked. The server does say how
    many tokens it read, though, and within one chain of tool calls the prompt
    only ever grows — so if it grew by less than the text just added could
    possibly cost, something was dropped.

    Only compared across turns with no user message between them, and that is
    not a nicety. Templates drop earlier reasoning at every new user turn, so a
    nudge or a reply shrinks the prompt legitimately, and comparing across one
    would cry wolf at exactly the moments a person is reading the run.

    It is a floor, not a meter: a drop small enough to hide inside the slack is
    missed. What it catches is a window that is simply too small for the run,
    which is the case worth telling someone about.
    """

    last_tokens: int | None = None
    sent: int = 0
    reported: bool = False

    def check(self, messages: list[dict[str, Any]], reply: _Assistant) -> str | None:
        added = messages[self.sent :]
        previous, self.last_tokens = self.last_tokens, reply.prompt_tokens
        if (
            self.reported
            or previous is None
            or reply.prompt_tokens is None
            or any(m.get("role") == "user" for m in added)
        ):
            return None
        floor = sum(_chars(m) for m in added) // _CHARS_PER_TOKEN_CEILING
        if reply.prompt_tokens >= previous + floor - _TRUNCATION_MARGIN_TOKENS:
            return None
        self.reported = True
        return (
            f"The model server read {reply.prompt_tokens} tokens of a conversation that "
            f"was at least {previous + floor}: it is dropping the oldest messages to fit "
            "its context window. The task is kept, but earlier tool output is gone. "
            "Raise WORKBENCH_INFERENCE_CONTEXT_TOKENS on the node if runs need more."
        )


async def _turn(
    client: httpx.AsyncClient, payload: dict[str, Any]
) -> AsyncIterator[AgentEvent | _Assistant]:
    """One request to the model: events as they arrive, then the assembled reply.

    Shaped like the backend protocol itself — any number of events, then
    exactly one terminal object — so the caller reads it the same way the
    runner reads a backend, and cannot see the reply before the events that
    led to it.
    """
    reply = _Assistant()
    text = _Buffer(RunEventKind.TEXT, TEXT_FLUSH_CHARS, TEXT_FLUSH_SECONDS)
    thinking = _Buffer(RunEventKind.THINKING, THINKING_FLUSH_CHARS, THINKING_FLUSH_SECONDS)

    async with client.stream("POST", "/chat/completions", json=payload) as response:
        if response.status_code >= 400:
            body = (await response.aread()).decode("utf-8", errors="replace")
            raise httpx.HTTPStatusError(
                f"{response.status_code}: {clip(body)}",
                request=response.request,
                response=response,
            )
        async for line in response.aiter_lines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                line = line[len("data:") :].strip()
            if line == "[DONE]":
                break
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Unreadable chunk from the model server: %r", line[:200])
                continue

            reply.model = chunk.get("model") or reply.model
            usage = chunk.get("usage") or {}
            if isinstance(usage.get("prompt_tokens"), int):
                reply.prompt_tokens = usage["prompt_tokens"]
            for choice in chunk.get("choices") or []:
                # `delta` when streaming, `message` when a server answered in
                # one piece despite being asked to stream. Both appear in the
                # wild; handling the second here is cheaper than detecting it.
                fragment, reasoned = _apply_delta(
                    choice.get("delta") or choice.get("message") or {}, reply
                )
                if fragment and (event := text.add(fragment)):
                    yield event
                if reasoned and (event := thinking.add(reasoned)):
                    yield event

    for buffer in (thinking, text):
        if event := buffer.flush():
            yield event
    yield reply


def _tool_arguments(call: dict[str, Any]) -> dict[str, Any] | str:
    """A call's arguments, or the raw text when they will not parse."""
    raw = (call.get("function") or {}).get("arguments") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return raw
    return parsed if isinstance(parsed, dict) else raw


def _session_path(token: str) -> Path:
    return sessions_dir() / f"{token}.json"


def _load_transcript(token: str | None) -> list[dict[str, Any]] | None:
    """An earlier run's messages, or None if there are none to be had.

    A token pointing at a file that is gone reads as "start fresh" rather than
    as an error: the alternative is a task whose every future run fails on a
    token read back from the same row, which is the trap the Claude adapter
    had to be taught to recover from.
    """
    if not token:
        return None
    path = _session_path(token)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        logger.warning("No usable transcript at %s; starting fresh.", path)
        return None
    return loaded if isinstance(loaded, list) else None


def _save_transcript(token: str, messages: list[dict[str, Any]]) -> None:
    """Write the conversation so far. Called every turn, not just at the end:
    a run killed by a deploy should still be continuable."""
    path = _session_path(token)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(messages), encoding="utf-8")
    except OSError:
        logger.exception("Could not write the transcript at %s.", path)


class LocalBackend:
    """Drives a model served on this machine or this network.

    Holds no state between runs, like the Claude adapter: continuity is the
    transcript named by the resume token, not this object.
    """

    @property
    def wants_endpoint(self) -> bool:
        """The whole point of this backend: it talks to whatever OpenAI-compatible
        server the runner picked, which may be another machine entirely."""
        return True

    @property
    def name(self) -> str:
        return BACKEND_NAME

    def credential_status(self) -> CredentialStatus:
        """Whether a run started now would reach a model, and which one.

        The same question the Claude adapter answers about a login, asked in
        local terms — there is no credential, so what matters is whether
        anything is listening and whether it has the model loaded. Both
        failures are ordinary and both are reported rather than raised.
        """
        base = inference_base_url()
        host = httpx.URL(base).netloc.decode() or base
        wanted = local_model()

        try:
            with _probe() as probe:
                response = probe.get("/models")
                response.raise_for_status()
                payload = response.json()
        except httpx.ConnectError:
            return CredentialStatus(
                backend=BACKEND_NAME,
                logged_in=False,
                method=CREDENTIAL_LOCAL,
                detail=(
                    f"No model server answered at {base}. Start one on this machine, "
                    "or point WORKBENCH_INFERENCE_URL at the node serving it."
                ),
            )
        except (httpx.HTTPError, ValueError) as exc:
            return CredentialStatus(
                backend=BACKEND_NAME,
                logged_in=False,
                method=CREDENTIAL_UNKNOWN,
                detail=f"The model server at {base} did not answer usefully: {exc}",
            )

        served = [
            str(item.get("id"))
            for item in (payload.get("data") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        # A served name may carry a tag the configured name omits
        # (`qwen2.5-coder:7b` vs `qwen2.5-coder:7b-instruct-q4_K_M`), so a
        # prefix match is the honest test rather than equality.
        if not any(name == wanted or name.startswith(f"{wanted}:") for name in served):
            available = ", ".join(served) or "none"
            return CredentialStatus(
                backend=BACKEND_NAME,
                logged_in=False,
                method=CREDENTIAL_LOCAL,
                account=host,
                detail=(
                    f"{host} is serving {available}, but not {wanted}. Pull it there "
                    f"(`ollama pull {wanted}`) or set WORKBENCH_LOCAL_MODEL to one it has."
                ),
            )
        return CredentialStatus(
            backend=BACKEND_NAME,
            logged_in=True,
            method=CREDENTIAL_LOCAL,
            account=host,
            detail=f"Serving {wanted} at {host}. Nothing to bill and nothing to expire.",
        )

    async def run(self, request: AgentRequest) -> AgentStream:
        """Drive the loop: ask, run what it asks for, ask again.

        The turn count, not the wall clock, is what ends a run that is going
        nowhere — and the two termination cases are deliberately different. A
        plan run ends when it submits a plan; a working run ends when the
        model replies without asking for a tool, which is the natural shape of
        "I am done" and needs no cooperation from a small model beyond
        stopping.
        """
        phase = request.phase
        if phase is RunPhase.REVIEW:
            # Not offered as a reviewer (see `registry.can_review`); refused here
            # too, so a review that reaches this backend anyway fails with a
            # reason rather than running as a plan with no way to deliver.
            yield AgentUnavailable(
                "The local backend does not review work. Choose a reviewer that can, "
                "such as claude, in the project's Agent panel."
            )
            return
        model = request.model or local_model()
        # What the runner picked, else this machine's own configuration. The
        # fallback is what keeps a single-machine install working with no nodes
        # registered at all.
        base_url = request.endpoint or inference_base_url()
        token = request.resume_token or uuid.uuid4().hex
        context = ToolContext(
            worktree=request.worktree,
            api_base=f"http://127.0.0.1:{port()}",
            run_id=request.run_id,
            task_id=request.task_id,
            project_id=request.project_id,
            head_at_start=_head_of(request.worktree),
        )

        messages = _load_transcript(request.resume_token)
        begin = _BEGIN
        restarted = False
        if messages is not None and (size := _estimated_tokens(messages)) > _resume_budget():
            yield AgentEvent(
                RunEventKind.NOTICE,
                {
                    "text": (
                        f"The earlier conversation is about {size:,} tokens — too long to "
                        "continue in this model's context window. Starting a fresh session "
                        "in the same worktree."
                    )
                },
            )
            messages = None
            token = uuid.uuid4().hex
            begin = f"{_BEGIN}\n\n{_AFTER_A_LONG_ATTEMPT}"
            restarted = True
        if messages is None:
            if request.resume_token and not restarted:
                yield AgentEvent(
                    RunEventKind.NOTICE,
                    {"text": "The earlier conversation could not be found; starting fresh."},
                )
                token = uuid.uuid4().hex
            # The task goes in the system message, not the first user turn —
            # see `_pinned`. A continuation keeps the one its session began with.
            messages = [
                {"role": "system", "content": _pinned(system_prompt(phase), request.prompt)}
            ]
            messages.append({"role": "user", "content": begin})
        else:
            messages.append({"role": "user", "content": request.prompt})

        tools = tools_for(phase)
        turns = 0
        answered: str | None = None
        plan: PlanSubmitted | None = None
        failures = 0
        started = False
        nudges = 0
        #: Whether the loop ended because the agent stopped, as opposed to
        #: because it ran out of turns. Tracked rather than inferred: a run
        #: that stalled, was nudged, and then ground on to the turn limit has
        #: an `answered` from the stall, and inferring from that would report
        #: a run cut short as one that chose to stop — which is exactly the
        #: distinction `stopped_early` exists to make, since the runner
        #: distrusts a self-reported outcome without it.
        ended_cleanly = False
        window = _Window()

        async with _client(base_url) as client:
            cap = _max_turns(phase)
            notice_at = max(1, int(cap * BUDGET_NOTICE_FRACTION))
            while turns < cap:
                turns += 1
                offered = tools
                if turns == notice_at and (note := _budget_note(phase, cap - turns + 1)):
                    _tell(messages, note)
                    yield AgentEvent(RunEventKind.NOTICE, {"text": f"Told the model: {note}"})
                if phase is RunPhase.PLAN and turns == cap and cap > 1:
                    _tell(messages, _FINAL_PLAN_TURN)
                    offered = [t for t in tools if t["function"]["name"] == "submit_plan"]
                    yield AgentEvent(
                        RunEventKind.NOTICE,
                        {"text": "Last planning turn: offering only submit_plan."},
                    )
                payload = {
                    "model": model,
                    "messages": messages,
                    "tools": offered,
                    "stream": True,
                    # So the last chunk says how much the server actually read.
                    "stream_options": {"include_usage": True},
                }

                reply = _Assistant()
                try:
                    async for item in _turn(client, payload):
                        if isinstance(item, AgentEvent):
                            yield item
                        else:
                            reply = item
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    if not started:
                        # Nothing was attempted: there is no summary to write,
                        # no diff to take, and nothing to record but the fact
                        # that no model answered.
                        yield AgentUnavailable(f"No model server answered at {base_url}: {exc}")
                        return
                    yield AgentFailed(
                        f"The model server stopped answering: {exc}",
                        resume_token=token,
                        model=model,
                        num_turns=turns,
                    )
                    return
                except httpx.HTTPStatusError as exc:
                    # A tool call the *server* could not parse, reported as its
                    # own 500. Ollama does this when a model's call is cut off
                    # or malformed — seen when a 4k window ran out mid-call —
                    # and it is the same mistake as arguments that will not
                    # parse here, so it gets the same treatment: say so, count
                    # it, try again, rather than ending a run over one turn.
                    if started and _UNPARSED_TOOL_CALL in str(exc):
                        failures += 1
                        if failures >= MAX_CONSECUTIVE_TOOL_FAILURES:
                            yield AgentFailed(
                                "The model could not form a tool call the server could "
                                f"parse: {failures} attempts in a row.",
                                resume_token=token,
                                model=model,
                                num_turns=turns,
                            )
                            return
                        yield AgentEvent(
                            RunEventKind.NOTICE,
                            {"text": "The server could not parse the model's tool call; retrying."},
                        )
                        messages.append({"role": "user", "content": _RESEND_TOOL_CALL})
                        _save_transcript(token, messages)
                        continue
                    if not started:
                        yield AgentUnavailable(f"The model server refused the request: {exc}")
                        return
                    yield AgentFailed(
                        f"The model server failed mid-run: {exc}",
                        resume_token=token,
                        model=model,
                        num_turns=turns,
                    )
                    return
                except httpx.HTTPError as exc:
                    if not started:
                        yield AgentUnavailable(f"The model server refused the request: {exc}")
                        return
                    yield AgentFailed(
                        f"The model server failed mid-run: {exc}",
                        resume_token=token,
                        model=model,
                        num_turns=turns,
                    )
                    return

                started = True
                model = reply.model or model
                if dropped := window.check(messages, reply):
                    yield AgentEvent(RunEventKind.NOTICE, {"text": dropped})
                window.sent = len(messages)

                if not reply.tool_calls:
                    # Before believing "no tool calls" means "done", check
                    # whether it wrote one out as text. See
                    # `_tool_calls_from_text` — this is the single most common
                    # thing a small model gets wrong.
                    recovered = _tool_calls_from_text(reply.text, set(tool_names_for(phase)))
                    if recovered:
                        yield AgentEvent(
                            RunEventKind.NOTICE,
                            {
                                "text": (
                                    f"The model wrote {len(recovered)} tool call(s) as text "
                                    "rather than calling them; recovered."
                                )
                            },
                        )
                        reply.tool_calls = recovered
                        reply.text = ""

                messages.append(reply.as_message())
                _save_transcript(token, messages)

                if not reply.tool_calls:
                    answered = reply.text.strip()
                    # A reply with no tool calls is how a capable model says it
                    # is done. A small one says it the same way after a failed
                    # call — narrating what it intends to do next instead of
                    # doing it. If the worktree is untouched, nothing has been
                    # done, so this is not an ending; it is a stall, and one
                    # push is worth more than a run that reports nothing.
                    if (
                        phase is RunPhase.EXECUTE
                        and nudges < MAX_NUDGES
                        # A run that has reported an outcome stopped on
                        # purpose. Asking it to carry on is not a nudge, it is
                        # an argument with a decision — and after a question it
                        # is worse, because the thing it is waiting for is a
                        # person, not a reminder.
                        and context.used.isdisjoint(OUTCOME_TOOLS)
                        and nothing_happened(context)
                    ):
                        nudges += 1
                        yield AgentEvent(
                            RunEventKind.NOTICE,
                            {
                                "text": (
                                    "The model stopped without changing anything; "
                                    f"asking it to continue ({nudges}/{MAX_NUDGES})."
                                )
                            },
                        )
                        messages.append({"role": "user", "content": _NUDGE})
                        _save_transcript(token, messages)
                        continue
                    if (
                        phase is RunPhase.EXECUTE
                        and nudges < MAX_NUDGES
                        and context.used.isdisjoint(OUTCOME_TOOLS)
                        and uncommitted_work(context)
                    ):
                        # The other way to stop early: the work is done, or half
                        # done, and left lying in the worktree. Run 66 ended
                        # like this — edits made, nothing committed, no outcome
                        # — so nothing was published and nobody was told.
                        nudges += 1
                        yield AgentEvent(
                            RunEventKind.NOTICE,
                            {
                                "text": (
                                    "The model stopped with uncommitted changes and no "
                                    f"outcome; asking it to finish ({nudges}/{MAX_NUDGES})."
                                )
                            },
                        )
                        messages.append({"role": "user", "content": _FINISH_NUDGE})
                        _save_transcript(token, messages)
                        continue
                    if (
                        phase is RunPhase.PLAN
                        and not answered
                        and plan is None
                        and nudges < MAX_NUDGES
                        and turns < cap
                    ):
                        nudges += 1
                        yield AgentEvent(
                            RunEventKind.NOTICE,
                            {
                                "text": (
                                    "The model stopped with no plan and nothing to say; "
                                    f"asking it to continue ({nudges}/{MAX_NUDGES})."
                                )
                            },
                        )
                        messages.append({"role": "user", "content": _PLAN_NUDGE})
                        _save_transcript(token, messages)
                        continue
                    if phase is not RunPhase.CONVERSATION:
                        ended_cleanly = True
                        break
                    # A conversation waits for the next thing typed, which is
                    # the whole point of one. Nothing typed ends it.
                    if request.inputs is None:
                        ended_cleanly = True
                        break
                    try:
                        typed = await anext(request.inputs)
                    except StopAsyncIteration:
                        ended_cleanly = True
                        break
                    messages.append({"role": "user", "content": typed})
                    _save_transcript(token, messages)
                    continue

                # A small model composes whole scripts: read, edit, commit,
                # report, all in one message, before any of them has run. The
                # second real run did exactly that, and its edit was authored
                # against a file it had not read yet. So the batch stops at the
                # first failure — every call after one is reasoning from a
                # result that never happened.
                batch_failed = False
                for call in reply.tool_calls:
                    name = str((call.get("function") or {}).get("name") or "")
                    call_id_early = call.get("id") or f"call_{turns}"
                    if batch_failed:
                        skipped = (
                            f"Not run: {name} was queued behind a call that failed. "
                            "Look at that result and decide again."
                        )
                        yield AgentEvent(
                            RunEventKind.TOOL_RESULT,
                            {"id": call_id_early, "text": skipped, "is_error": True},
                        )
                        # Still answered, because a tool call with no matching
                        # result is a malformed conversation to a strict server.
                        messages.append(
                            {"role": "tool", "tool_call_id": call_id_early, "content": skipped}
                        )
                        continue
                    arguments = _tool_arguments(call)
                    call_id = call.get("id") or f"call_{turns}"

                    # Logged even when the arguments are unusable: a run that
                    # went nowhere because the model kept sending broken JSON
                    # should show what it kept sending.
                    yield AgentEvent(
                        RunEventKind.TOOL_USE,
                        {
                            "id": call_id,
                            "name": name,
                            "input": (
                                arguments if isinstance(arguments, dict) else {"raw": arguments}
                            ),
                        },
                    )
                    outcome: PlanSubmitted | ToolResult
                    if isinstance(arguments, str):
                        failures += 1
                        outcome = ToolResult(
                            "Those arguments were not valid JSON. Send the tool call "
                            "again with a JSON object matching the schema.",
                            is_error=True,
                        )
                    else:
                        outcome = dispatch(phase, name, arguments, context)
                        failed = isinstance(outcome, ToolResult) and outcome.is_error
                        failures = failures + 1 if failed else 0

                    if isinstance(outcome, PlanSubmitted):
                        plan = outcome
                        yield AgentEvent(
                            RunEventKind.TOOL_RESULT,
                            {"id": call_id, "text": "Plan submitted.", "is_error": False},
                        )
                        break

                    yield AgentEvent(
                        RunEventKind.TOOL_RESULT,
                        {"id": call_id, "text": outcome.text, "is_error": outcome.is_error},
                    )
                    messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": outcome.text}
                    )
                    batch_failed = outcome.is_error

                _save_transcript(token, messages)
                if plan is not None:
                    ended_cleanly = True
                    break
                if failures >= MAX_CONSECUTIVE_TOOL_FAILURES:
                    yield AgentEvent(
                        RunEventKind.NOTICE,
                        {"text": f"{failures} tool calls in a row failed; giving up."},
                    )
                    yield AgentFailed(
                        f"The model could not use its tools: {failures} consecutive failed calls.",
                        resume_token=token,
                        model=model,
                        num_turns=turns,
                    )
                    return

        stopped_early = not ended_cleanly
        if stopped_early:
            yield AgentEvent(
                RunEventKind.NOTICE,
                {"text": f"Stopped at the {turns}-turn limit for this phase."},
            )

        if phase is RunPhase.PLAN:
            subtasks: list[SubtaskProposal] | None = plan.subtasks if plan else []
            text = plan.plan if plan else (answered or "")
            if plan is None and text:
                # It planned in prose and never called the tool. The plan is
                # still worth keeping — a person reads it either way — so this
                # is a notice rather than a failure.
                yield AgentEvent(
                    RunEventKind.NOTICE,
                    {"text": "The model wrote a plan without calling submit_plan."},
                )
            if not text:
                yield AgentFailed(
                    "The planning run produced no plan.",
                    resume_token=token,
                    model=model,
                    num_turns=turns,
                )
                return
            yield AgentFinished(
                text=text,
                resume_token=token,
                model=model,
                num_turns=turns,
                proposed_subtasks=subtasks,
                stopped_early=stopped_early,
            )
            return

        yield AgentFinished(
            text=answered or "",
            resume_token=token,
            model=model,
            num_turns=turns,
            stopped_early=stopped_early,
        )
