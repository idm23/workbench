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
    PlanSubmitted,
    ToolContext,
    ToolResult,
    clip,
    dispatch,
    nothing_happened,
    tool_names_for,
    tools_for,
)
from workbench.config import (
    inference_base_url,
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

#: How many malformed tool calls in a row before this is not going to work.
#: Small models emit unparseable arguments; they usually recover when told,
#: and when they do not they do it forever.
MAX_CONSECUTIVE_TOOL_FAILURES = 4

#: Text is buffered into events rather than emitted per delta: every event is
#: a row, and a row per token would be tens of thousands of them for one run.
#: Flushed on either bound so a slow model still shows something moving.
TEXT_FLUSH_CHARS = 400
TEXT_FLUSH_SECONDS = 2.0

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


def _client() -> httpx.AsyncClient:
    """The connection to the model server.

    A function rather than an inline constructor so a test can hand back one
    wired to a transport instead of a socket — the same reason the Claude
    adapter keeps `_cli_path()` separate from the code that uses it.
    """
    return httpx.AsyncClient(base_url=inference_base_url(), timeout=inference_timeout_seconds())


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
        "Nobody is attached to this session. Do not ask questions — decide, say "
        "which interpretation you chose, and carry on. Work in small steps, and "
        "check what you changed afterwards."
    )
    if phase is RunPhase.PLAN:
        return (
            f"{common}\n"
            "\n"
            "This is a planning run and you have read-only tools only. When your "
            "investigation is done, call submit_plan exactly once with the plan "
            "in prose. That ends the run."
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
        "tool calls: that reply ends the run and is what a reviewer reads."
    )


@dataclass
class _Assistant:
    """One assembled reply, whatever order the deltas arrived in."""

    text: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.text}
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
    """Coalesces streamed text into events worth storing as rows."""

    def __init__(self, kind: RunEventKind) -> None:
        self._kind = kind
        self._text = ""
        self._since = datetime.now(UTC)

    def add(self, fragment: str) -> AgentEvent | None:
        self._text += fragment
        elapsed = (datetime.now(UTC) - self._since).total_seconds()
        if len(self._text) >= TEXT_FLUSH_CHARS or elapsed >= TEXT_FLUSH_SECONDS:
            return self.flush()
        return None

    def flush(self) -> AgentEvent | None:
        text, self._text = self._text, ""
        self._since = datetime.now(UTC)
        if not text.strip():
            return None
        return AgentEvent(self._kind, {"text": clip(text)})


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
    text = _Buffer(RunEventKind.TEXT)
    thinking = _Buffer(RunEventKind.THINKING)

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
        model = request.model or local_model()
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
        if messages is None:
            if request.resume_token:
                yield AgentEvent(
                    RunEventKind.NOTICE,
                    {"text": "The earlier conversation could not be found; starting fresh."},
                )
                token = uuid.uuid4().hex
            messages = [{"role": "system", "content": system_prompt(phase)}]
        messages.append({"role": "user", "content": request.prompt})

        tools = tools_for(phase)
        turns = 0
        answered: str | None = None
        plan: PlanSubmitted | None = None
        failures = 0
        started = False
        nudges = 0

        async with _client() as client:
            while turns < _max_turns(phase):
                turns += 1
                payload = {
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "stream": True,
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
                        yield AgentUnavailable(
                            f"No model server answered at {inference_base_url()}: {exc}"
                        )
                        return
                    yield AgentFailed(
                        f"The model server stopped answering: {exc}",
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
                    if phase is not RunPhase.CONVERSATION:
                        break
                    # A conversation waits for the next thing typed, which is
                    # the whole point of one. Nothing typed ends it.
                    if request.inputs is None:
                        break
                    try:
                        typed = await anext(request.inputs)
                    except StopAsyncIteration:
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

        stopped_early = turns >= _max_turns(phase) and plan is None and answered is None
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
