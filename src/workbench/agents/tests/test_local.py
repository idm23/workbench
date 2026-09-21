"""The local backend: the loop, and where it decides a run is over.

No model and no server here — a `MockTransport` answers with scripted
completions, which is the whole point of a backend that talks HTTP rather than
an SDK: the wire format is the seam, and it can be written down.

What is worth testing is not the happy path so much as the endings. A run
finishes when a model stops asking for tools, gives up when it cannot form a
tool call, and comes back as *unavailable* rather than *failed* when nothing
answered at all — because that distinction is what tells someone whether to
look at their task or at their GPU.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx

from workbench.agents import local as backend_module
from workbench.agents.local import LocalBackend
from workbench.agents.protocol import (
    CREDENTIAL_LOCAL,
    CREDENTIAL_UNKNOWN,
    AgentEvent,
    AgentFailed,
    AgentFinished,
    AgentRequest,
    AgentUnavailable,
)
from workbench.agents.tests.helpers import drain
from workbench.database.models import RunEventKind, RunPhase


def a_request(**overrides: Any) -> AgentRequest:
    fields: dict[str, Any] = {
        "worktree": Path("/tmp/worktree"),
        "phase": RunPhase.EXECUTE,
        "prompt": "Do the thing",
    }
    return AgentRequest(**(fields | overrides))


def a_repository(tmp_path) -> Path:
    """A worktree that is a real checkout.

    Two of the guards below decide from git — whether the run changed anything
    — and both answer "cannot tell" outside a repository, which is correct and
    makes for a test that passes without exercising them.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("x = 1\n")
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"]):
        subprocess.run(["git", *argv], cwd=worktree, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-qm", "first"],
        cwd=worktree,
        check=True,
        capture_output=True,
    )
    return worktree


def chunk(**delta: Any) -> dict[str, Any]:
    return {"model": "test-model", "choices": [{"delta": delta}]}


def tool_call(name: str, arguments: Any, call_id: str = "call-1", index: int = 0) -> dict[str, Any]:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return chunk(
        tool_calls=[{"index": index, "id": call_id, "function": {"name": name, "arguments": raw}}]
    )


def sse(*chunks: dict[str, Any]) -> bytes:
    body = "".join(f"data: {json.dumps(one)}\n\n" for one in chunks) + "data: [DONE]\n\n"
    return body.encode()


def stub(*responses: bytes | Exception, captured: dict[str, Any] | None = None):
    """A scripted model server, as a replacement for `_client`.

    A script that runs out keeps answering with its last response rather than
    stopping. That is deliberate: it is exactly what a model that will not
    stop asking for tools looks like, which is the case the turn limit exists
    for.
    """
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.setdefault("payloads", []).append(json.loads(request.content))
        body = remaining[0] if len(remaining) == 1 else remaining.pop(0)
        if isinstance(body, Exception):
            raise body
        return httpx.Response(200, content=body)

    def factory(base_url: str | None = None) -> httpx.AsyncClient:
        # The signature mirrors `_client`, which now takes the endpoint the
        # runner picked. The scripted server ignores it: what is under test is
        # the loop, not the address.
        return httpx.AsyncClient(
            base_url=base_url or "http://model.test/v1", transport=httpx.MockTransport(handler)
        )

    return factory


def probing(payload: dict[str, Any] | Exception, status: int = 200):
    """A replacement for `_probe`, the credential check's connection."""

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, Exception):
            raise payload
        return httpx.Response(status, json=payload)

    def factory() -> httpx.Client:
        return httpx.Client(base_url="http://model.test/v1", transport=httpx.MockTransport(handler))

    return factory


def events(items: list[Any], kind: RunEventKind) -> list[dict[str, Any]]:
    return [item.payload for item in items if isinstance(item, AgentEvent) and item.kind is kind]


def test_a_reply_with_no_tool_calls_ends_the_run(monkeypatch):
    """The natural shape of "I am done", and the one that needs no
    cooperation from a small model beyond stopping."""
    monkeypatch.setattr(backend_module, "_client", stub(sse(chunk(content="I changed app.py."))))

    items = drain(LocalBackend().run(a_request()))

    assert isinstance(items[-1], AgentFinished)
    assert items[-1].text == "I changed app.py."
    assert items[-1].model == "test-model"
    assert items[-1].num_turns == 1


def test_text_arrives_as_events_before_the_outcome(monkeypatch):
    """The run page is fed from these, and a model at 15 tokens a second is
    exactly the case where nothing appearing until the end is unbearable."""
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(sse(chunk(content="Looking"), chunk(content=" at it."))),
    )

    items = drain(LocalBackend().run(a_request()))

    assert "".join(payload["text"] for payload in events(items, RunEventKind.TEXT)) == (
        "Looking at it."
    )
    assert isinstance(items[-1], AgentFinished)


def test_reasoning_is_coalesced_harder_than_text(monkeypatch):
    """A reasoning model talks to itself at length: one qwen3 run here made
    three tool calls and over a hundred rows of monologue, all of them kept
    forever and scrolled past by whoever wanted to see what it did."""
    assert backend_module.THINKING_FLUSH_CHARS > backend_module.TEXT_FLUSH_CHARS
    assert backend_module.THINKING_FLUSH_SECONDS > backend_module.TEXT_FLUSH_SECONDS


def test_a_long_monologue_is_not_one_row_per_thought(monkeypatch):
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(
                *[chunk(thinking="Let me think about this. " * 4) for _ in range(20)],
                chunk(content="done"),
            )
        ),
    )

    items = drain(LocalBackend().run(a_request()))
    thoughts = events(items, RunEventKind.THINKING)

    # Twenty chunks of reasoning, a handful of rows.
    assert 0 < len(thoughts) <= 4


def test_reasoning_is_recorded_as_thinking(monkeypatch):
    """Three servers spell this field three ways; all three mean thinking."""
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(
                chunk(thinking="ollama says this"),
                chunk(reasoning_content="vllm says this"),
                chunk(content="done"),
            )
        ),
    )

    items = drain(LocalBackend().run(a_request()))
    thought = "".join(payload["text"] for payload in events(items, RunEventKind.THINKING))

    assert "ollama says this" in thought
    assert "vllm says this" in thought


def test_a_tool_call_runs_and_its_result_goes_back_to_the_model(monkeypatch, tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("x = 1\n")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(tool_call("read_file", {"path": "app.py"})),
            sse(chunk(content="It sets x.")),
            captured=captured,
        ),
    )

    items = drain(LocalBackend().run(a_request(worktree=worktree)))

    assert events(items, RunEventKind.TOOL_USE)[0]["name"] == "read_file"
    assert "x = 1" in events(items, RunEventKind.TOOL_RESULT)[0]["text"]
    # The second request carries the assistant's call and the tool's answer,
    # which is what makes it a conversation rather than two disconnected asks.
    second = captured["payloads"][1]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert second[-1]["role"] == "tool"
    assert isinstance(items[-1], AgentFinished)


def test_tool_call_fragments_are_assembled_by_index(monkeypatch, tmp_path):
    """Streaming splits one call across chunks — the name in one, the
    arguments a few characters at a time. Assembling by arrival order instead
    produces a call whose arguments are two calls concatenated."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("x = 1\n")
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(
                chunk(
                    tool_calls=[
                        {"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": ""}}
                    ]
                ),
                chunk(tool_calls=[{"index": 0, "function": {"arguments": '{"path": '}}]),
                chunk(tool_calls=[{"index": 0, "function": {"arguments": '"app.py"}'}}]),
            ),
            sse(chunk(content="done")),
        ),
    )

    items = drain(LocalBackend().run(a_request(worktree=worktree)))

    assert events(items, RunEventKind.TOOL_USE)[0]["input"] == {"path": "app.py"}
    assert not events(items, RunEventKind.TOOL_RESULT)[0]["is_error"]


def test_the_plan_phase_is_sent_no_tool_that_writes(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(sse(tool_call("submit_plan", {"plan": "Do it"})), captured=captured),
    )

    drain(LocalBackend().run(a_request(phase=RunPhase.PLAN)))
    offered = {schema["function"]["name"] for schema in captured["payloads"][0]["tools"]}

    assert "run_command" not in offered
    assert "write_file" not in offered


def test_submitting_a_plan_ends_the_planning_run(monkeypatch, tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("x = 1\n")
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            # A look first: `submit_plan` refuses to be the first thing a run
            # does, because a plan written from the task title alone is not one.
            sse(tool_call("read_file", {"path": "app.py"})),
            sse(
                tool_call(
                    "submit_plan",
                    {
                        "plan": "Add the endpoint.",
                        "subtasks": [
                            {"title": "Add it", "body": "In app.py", "ready_to_execute": True}
                        ],
                    },
                )
            ),
        ),
    )

    outcome = drain(LocalBackend().run(a_request(phase=RunPhase.PLAN, worktree=worktree)))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.text == "Add the endpoint."
    assert outcome.proposed_subtasks is not None
    assert outcome.proposed_subtasks[0].title == "Add it"


def test_a_plan_written_in_prose_is_kept_with_a_notice(monkeypatch):
    """A person reads the plan either way. Throwing it away because the model
    did not reach for the tool would waste a run that did the thinking."""
    monkeypatch.setattr(
        backend_module, "_client", stub(sse(chunk(content="First, read config.py.")))
    )

    items = drain(LocalBackend().run(a_request(phase=RunPhase.PLAN)))

    assert isinstance(items[-1], AgentFinished)
    assert items[-1].text == "First, read config.py."
    assert items[-1].proposed_subtasks == []
    notices = events(items, RunEventKind.NOTICE)
    assert any("without calling submit_plan" in one["text"] for one in notices)


def test_a_planning_run_that_produced_nothing_is_a_failure(monkeypatch):
    monkeypatch.setattr(backend_module, "_client", stub(sse(chunk(content="   "))))

    outcome = drain(LocalBackend().run(a_request(phase=RunPhase.PLAN)))[-1]

    assert isinstance(outcome, AgentFailed)
    assert "no plan" in outcome.message


def test_nothing_listening_is_unavailable_not_failed(monkeypatch):
    """Nothing was attempted: no summary to write, no diff to take. The
    distinction is what tells someone to look at their GPU rather than at
    their task."""
    monkeypatch.setattr(backend_module, "_client", stub(httpx.ConnectError("refused")))

    outcome = drain(LocalBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentUnavailable)
    assert "No model server answered" in outcome.message


def test_a_server_that_dies_mid_run_is_a_failure(monkeypatch, tmp_path):
    """Work may already have happened — there can be commits in the worktree."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("x = 1\n")
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(sse(tool_call("read_file", {"path": "app.py"})), httpx.ConnectError("gone")),
    )

    outcome = drain(LocalBackend().run(a_request(worktree=worktree)))[-1]

    assert isinstance(outcome, AgentFailed)
    assert outcome.resume_token is not None


def test_a_refused_request_before_anything_ran_is_unavailable(monkeypatch):
    """A model name the server does not have looks like this, and it is an
    install problem rather than a run that went wrong."""
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            httpx.HTTPStatusError(
                "404",
                request=httpx.Request("POST", "http://model.test/v1/chat/completions"),
                response=httpx.Response(404),
            )
        ),
    )

    outcome = drain(LocalBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentUnavailable)


def test_malformed_arguments_are_fed_back_before_giving_up(monkeypatch):
    """Small models send unparseable arguments; they usually recover when
    told, and when they do not they do it forever."""
    monkeypatch.setattr(backend_module, "_client", stub(sse(tool_call("read_file", "{not json"))))

    items = drain(LocalBackend().run(a_request()))
    results = events(items, RunEventKind.TOOL_RESULT)

    assert "not valid JSON" in results[0]["text"]
    assert isinstance(items[-1], AgentFailed)
    assert "consecutive failed calls" in items[-1].message


def test_the_turn_limit_ends_a_run_that_will_not_stop(monkeypatch, tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("x = 1\n")
    monkeypatch.setattr(backend_module, "MAX_TURNS_EXECUTE", 3)
    monkeypatch.setattr(
        backend_module, "_client", stub(sse(tool_call("read_file", {"path": "app.py"})))
    )

    items = drain(LocalBackend().run(a_request(worktree=worktree)))

    assert isinstance(items[-1], AgentFinished)
    assert items[-1].stopped_early
    assert items[-1].num_turns == 3
    assert any("turn limit" in one["text"] for one in events(items, RunEventKind.NOTICE))


def test_the_transcript_is_written_and_resumed(monkeypatch):
    """A local endpoint keeps no session, so this backend has to — which is
    also why deleting a worktree cannot orphan the conversation."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        backend_module, "_client", stub(sse(chunk(content="first")), captured=captured)
    )

    first = drain(LocalBackend().run(a_request()))[-1]

    assert isinstance(first, AgentFinished)
    assert first.resume_token is not None
    assert backend_module._session_path(first.resume_token).is_file()

    monkeypatch.setattr(
        backend_module, "_client", stub(sse(chunk(content="second")), captured=captured)
    )
    drain(LocalBackend().run(a_request(prompt="And now this", resume_token=first.resume_token)))

    resumed = captured["payloads"][1]["messages"]
    # The original task is still pinned in the system message; the new message
    # arrives as a user turn after everything that was said.
    assert "Do the thing" in resumed[0]["content"]
    assert [message["content"] for message in resumed[-2:]] == ["first", "And now this"]


def test_a_transcript_that_is_gone_starts_fresh_rather_than_failing(monkeypatch):
    """The token is read back from the same row on every future attempt, so
    failing on it would brick the task for good — the trap the Claude adapter
    had to be taught to climb out of."""
    monkeypatch.setattr(backend_module, "_client", stub(sse(chunk(content="fresh"))))

    items = drain(LocalBackend().run(a_request(resume_token="nothing-here")))

    assert isinstance(items[-1], AgentFinished)
    assert items[-1].resume_token != "nothing-here"
    assert any("starting fresh" in one["text"] for one in events(items, RunEventKind.NOTICE))


def test_a_conversation_waits_for_what_is_typed_next(monkeypatch):
    async def typed():
        yield "and what about tests?"

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(sse(chunk(content="here")), sse(chunk(content="covered")), captured=captured),
    )

    outcome = drain(LocalBackend().run(a_request(phase=RunPhase.CONVERSATION, inputs=typed())))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.text == "covered"
    assert captured["payloads"][1]["messages"][-1]["content"] == "and what about tests?"


def test_a_tool_call_written_as_text_is_recovered(monkeypatch, tmp_path):
    """What the first real 7B run did on turn one. Ollama's parser only knows
    Qwen's tagged form, so an untagged call arrives as prose — and a loop that
    reads "no tool calls" as "finished" records the JSON as a summary and calls
    the run a success."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("x = 1\n")
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(chunk(content='{"name": "read_file", "arguments": {"path": "app.py"}}')),
            sse(chunk(content="It sets x to 1.")),
        ),
    )

    items = drain(LocalBackend().run(a_request(worktree=worktree)))

    assert events(items, RunEventKind.TOOL_USE)[0]["name"] == "read_file"
    assert "x = 1" in events(items, RunEventKind.TOOL_RESULT)[0]["text"]
    assert any("as text" in one["text"] for one in events(items, RunEventKind.NOTICE))
    assert items[-1].text == "It sets x to 1."


def test_the_tagged_form_is_recovered_too(monkeypatch, tmp_path):
    """Qwen's own spelling, which reaches us untagged only when the server's
    parser has already had a go at it."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("x = 1\n")
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(
                chunk(
                    content=(
                        '<tool_call>{"name": "read_file", "parameters": '
                        '{"path": "app.py"}}</tool_call>'
                    )
                )
            ),
            sse(chunk(content="done")),
        ),
    )

    items = drain(LocalBackend().run(a_request(worktree=worktree)))

    assert events(items, RunEventKind.TOOL_USE)[0]["input"] == {"path": "app.py"}


def test_a_summary_that_merely_contains_json_is_still_a_summary(monkeypatch):
    """The guard against over-recovering, and the reason it keys on the tool
    name: a run that explains a JSON payload it wrote must be allowed to end."""
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(sse(chunk(content='I added a fixture: {"name": "widget", "arguments": 3}.'))),
    )

    outcome = drain(LocalBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.text.startswith("I added a fixture")


def test_a_batch_stops_at_its_first_failure(monkeypatch, tmp_path):
    """A small model composes whole scripts — read, edit, commit, report — in
    one message, before any of them has run. The second real 7B run did that,
    and its edit was written against a file it had not read yet. Everything
    queued behind a failure is reasoning from a result that never happened."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "app.py").write_text("x = 1\n")
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(
                chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {
                                "name": "edit_file",
                                "arguments": json.dumps(
                                    {"path": "app.py", "old_text": "y = 2", "new_text": "y = 3"}
                                ),
                            },
                        },
                        {
                            "index": 1,
                            "id": "c2",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {"path": "never.py", "content": "should not happen"}
                                ),
                            },
                        },
                    ]
                )
            ),
            sse(chunk(content="I will look first.")),
        ),
    )

    items = drain(LocalBackend().run(a_request(worktree=worktree)))
    results = events(items, RunEventKind.TOOL_RESULT)

    assert "quote it exactly" in results[0]["text"]
    assert "queued behind a call that failed" in results[1]["text"]
    assert not (worktree / "never.py").exists()


def test_a_finished_claim_that_changed_nothing_is_refused(monkeypatch, tmp_path):
    """The run that prompted this reported finished with an empty worktree:
    Workbench noticed there were no commits and declined to push, and nothing
    declined the claim itself — the task went to done."""
    worktree = a_repository(tmp_path)
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(tool_call("read_file", {"path": "app.py"}, call_id="c1")),
            sse(tool_call("report_outcome", {"outcome": "finished"}, call_id="c2")),
            sse(chunk(content="Nothing needed doing.")),
        ),
    )

    items = drain(LocalBackend().run(a_request(worktree=worktree)))
    results = events(items, RunEventKind.TOOL_RESULT)

    assert "exactly as you found it" in results[1]["text"]
    assert results[1]["is_error"]


def test_a_run_that_stalled_and_then_ran_out_of_turns_says_so(monkeypatch, tmp_path):
    """The trap in inferring `stopped_early` from what was said last: a run
    that stalled, was nudged, and then ground on to the turn limit has a
    perfectly good final message from the stall. Reporting that as a clean
    ending would hand the runner a self-reported outcome it has no reason to
    distrust — which is the one thing this flag is for."""
    worktree = a_repository(tmp_path)
    monkeypatch.setattr(backend_module, "MAX_TURNS_EXECUTE", 4)
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(chunk(content="I will get to it shortly.")),
            sse(tool_call("read_file", {"path": "app.py"})),
        ),
    )

    outcome = drain(LocalBackend().run(a_request(worktree=worktree)))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.stopped_early


def test_a_run_that_finished_on_its_own_is_not_stopped_early(monkeypatch):
    monkeypatch.setattr(backend_module, "_client", stub(sse(chunk(content="All done."))))

    outcome = drain(LocalBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentFinished)
    assert not outcome.stopped_early


def test_a_run_costs_nothing(monkeypatch):
    """`total_cost_usd` is read as money. A local run spends a GPU, and
    inventing a figure for that would put a number in the wrong column."""
    monkeypatch.setattr(backend_module, "_client", stub(sse(chunk(content="done"))))

    outcome = drain(LocalBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.total_cost_usd is None


def test_the_credential_check_finds_the_model(monkeypatch):
    monkeypatch.setenv("WORKBENCH_LOCAL_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setattr(backend_module, "_probe", probing({"data": [{"id": "qwen2.5-coder:7b"}]}))

    status = LocalBackend().credential_status()

    assert status.logged_in
    assert status.method == CREDENTIAL_LOCAL
    assert status.login_command == ()


def test_a_served_tag_still_counts_as_the_model(monkeypatch):
    """`qwen2.5-coder:7b` configured, `qwen2.5-coder:7b:q4_K_M` served."""
    monkeypatch.setenv("WORKBENCH_LOCAL_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setattr(
        backend_module, "_probe", probing({"data": [{"id": "qwen2.5-coder:7b:q4_K_M"}]})
    )

    assert LocalBackend().credential_status().logged_in


def test_a_missing_model_says_how_to_pull_it(monkeypatch):
    monkeypatch.setenv("WORKBENCH_LOCAL_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setattr(backend_module, "_probe", probing({"data": [{"id": "llama3:8b"}]}))

    status = LocalBackend().credential_status()

    assert not status.logged_in
    assert status.method == CREDENTIAL_LOCAL
    assert "ollama pull qwen2.5-coder:7b" in status.detail
    assert "llama3:8b" in status.detail


def test_nothing_listening_reads_as_not_logged_in(monkeypatch):
    monkeypatch.setattr(backend_module, "_probe", probing(httpx.ConnectError("refused")))

    status = LocalBackend().credential_status()

    assert not status.logged_in
    assert status.method == CREDENTIAL_LOCAL
    assert "No model server answered" in status.detail


def test_a_probe_that_cannot_answer_is_unknown_rather_than_a_failure(monkeypatch):
    """A warning that fires when the checker itself breaks is one people learn
    to ignore, and then they miss the real one."""
    monkeypatch.setattr(backend_module, "_probe", probing({"data": []}, status=500))

    status = LocalBackend().credential_status()

    assert status.method == CREDENTIAL_UNKNOWN
    assert not status.logged_in


def test_a_run_that_asked_a_question_is_not_nudged(monkeypatch, tmp_path):
    """Found on a real run: the agent asked, stopped as instructed, and the
    loop spent its next two turns telling it to carry on. A run that reported
    an outcome stopped on purpose — and after a question the thing it waits
    for is a person, not a reminder."""
    worktree = a_repository(tmp_path)
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(tool_call("read_file", {"path": "app.py"}, call_id="c1")),
            sse(tool_call("ask_user", {"question": "Which shape did you want?"}, call_id="c2")),
            sse(chunk(content="Waiting to hear which you want.")),
        ),
    )
    monkeypatch.setattr(
        "httpx.post", lambda *a, **k: httpx.Response(204, request=httpx.Request("POST", "http://x"))
    )

    items = drain(LocalBackend().run(a_request(worktree=worktree)))
    notices = [one["text"] for one in events(items, RunEventKind.NOTICE)]

    assert not any("asking it to continue" in text for text in notices)
    assert isinstance(items[-1], AgentFinished)
    assert items[-1].text == "Waiting to hear which you want."


# --- What made local models lose their task ---------------------------------
#
# Found on task 50, and none of it looked like what it was. With a 4,096-token
# window and no reasoning carried between turns, gpt-oss read the right files
# and then answered a different question, asked whether there had been a user
# query yet, or replied with nothing at all. The window lives on the node (see
# `install_node._drop_in`); these are the loop's half.


def test_reasoning_goes_back_to_the_model_with_the_reply(monkeypatch, tmp_path):
    """Without it a model is shown each tool result minus the thought that
    asked for it, and has to reconstruct its own intent every turn."""
    worktree = a_repository(tmp_path)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(
                chunk(reasoning="I should read app.py before deciding."),
                tool_call("read_file", {"path": "app.py"}),
            ),
            sse(chunk(content="Done.")),
            captured=captured,
        ),
    )

    drain(LocalBackend().run(a_request(phase=RunPhase.PLAN, worktree=worktree)))

    replayed = [m for m in captured["payloads"][1]["messages"] if m["role"] == "assistant"]
    assert replayed[0]["reasoning"] == "I should read app.py before deciding."


def test_a_reply_without_reasoning_sends_none():
    reply = backend_module._Assistant(text="All done.")

    assert "reasoning" not in reply.as_message()


def test_an_empty_plan_turn_is_nudged_rather_than_failed(monkeypatch, tmp_path):
    """One turn with neither a tool call nor any text used to fail a plan run
    outright — the plan phase was the only one with no nudge at all."""
    worktree = a_repository(tmp_path)
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(tool_call("read_file", {"path": "app.py"})),
            sse(chunk(reasoning="Hmm.")),
            sse(tool_call("submit_plan", {"plan": "Change x.", "subtasks": []}, call_id="c2")),
        ),
    )

    items = drain(LocalBackend().run(a_request(phase=RunPhase.PLAN, worktree=worktree)))

    assert isinstance(items[-1], AgentFinished)
    assert items[-1].text == "Change x."
    assert any("asking it to continue" in n["text"] for n in events(items, RunEventKind.NOTICE))


def test_a_plan_run_is_told_its_budget_inside_the_tool_result(monkeypatch, tmp_path):
    """A capable model does not stop reading on its own; gpt-oss read the right
    files and ran into the cap twice with no plan. The note rides on the latest
    tool result rather than arriving as a user turn, because templates drop the
    reasoning from before the most recent user message."""
    worktree = a_repository(tmp_path)
    monkeypatch.setattr(backend_module, "MAX_TURNS_PLAN", 6)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(sse(tool_call("read_file", {"path": "app.py"})), captured=captured),
    )

    drain(LocalBackend().run(a_request(phase=RunPhase.PLAN, worktree=worktree)))

    told = captured["payloads"][2]["messages"]  # the third turn: 6 * 0.5
    assert told[-1]["role"] == "tool"
    assert "4 turns remain" in told[-1]["content"]
    assert [m["role"] for m in told].count("user") == 1  # still only the task


def test_the_last_plan_turn_offers_only_submit_plan(monkeypatch, tmp_path):
    """A model given one option and told to use it does. A plan written from a
    whole run's reading is worth more than "produced no plan"."""
    worktree = a_repository(tmp_path)
    monkeypatch.setattr(backend_module, "MAX_TURNS_PLAN", 3)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(tool_call("read_file", {"path": "app.py"})),
            sse(tool_call("read_file", {"path": "app.py"}, call_id="c2")),
            sse(tool_call("submit_plan", {"plan": "Do it.", "subtasks": []}, call_id="c3")),
            captured=captured,
        ),
    )

    items = drain(LocalBackend().run(a_request(phase=RunPhase.PLAN, worktree=worktree)))

    last = captured["payloads"][-1]
    assert [t["function"]["name"] for t in last["tools"]] == ["submit_plan"]
    assert "last turn" in last["messages"][-1]["content"]
    assert isinstance(items[-1], AgentFinished)
    assert items[-1].text == "Do it."


def test_an_execute_run_is_told_its_budget_but_keeps_its_tools(monkeypatch, tmp_path):
    worktree = a_repository(tmp_path)
    monkeypatch.setattr(backend_module, "MAX_TURNS_EXECUTE", 4)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(sse(tool_call("read_file", {"path": "app.py"})), captured=captured),
    )

    drain(LocalBackend().run(a_request(worktree=worktree)))

    assert "report_outcome" in captured["payloads"][1]["messages"][-1]["content"]
    assert len(captured["payloads"][-1]["tools"]) > 1


def test_the_task_is_pinned_where_truncation_cannot_reach(monkeypatch):
    """Ollama keeps system messages when it truncates and drops the oldest
    other message first — which is where the task used to be."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        backend_module, "_client", stub(sse(chunk(content="ok")), captured=captured)
    )

    drain(LocalBackend().run(a_request(prompt="Fix the approve button")))

    sent = captured["payloads"][0]["messages"]
    assert sent[0]["role"] == "system"
    assert "Fix the approve button" in sent[0]["content"]
    assert "Fix the approve button" not in sent[1]["content"]


def usage(prompt_tokens: int) -> dict[str, Any]:
    """The last chunk of a stream, when a server was asked to report usage."""
    return {"model": "test-model", "choices": [], "usage": {"prompt_tokens": prompt_tokens}}


def test_usage_is_asked_for(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        backend_module, "_client", stub(sse(chunk(content="ok")), captured=captured)
    )

    drain(LocalBackend().run(a_request()))

    assert captured["payloads"][0]["stream_options"] == {"include_usage": True}


def test_a_shrinking_prompt_inside_a_tool_chain_is_reported(monkeypatch, tmp_path):
    """The server read fewer tokens than it did a turn ago, although a file's
    worth of text was added in between: it dropped the start to fit."""
    worktree = a_repository(tmp_path)
    (worktree / "big.py").write_text("x = 1\n" * 800)
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(tool_call("read_file", {"path": "big.py"}), usage(3900)),
            sse(tool_call("read_file", {"path": "app.py"}, call_id="c2"), usage(3100)),
            sse(chunk(content="done"), usage(3150)),
        ),
    )

    items = drain(LocalBackend().run(a_request(phase=RunPhase.PLAN, worktree=worktree)))

    dropped = [n for n in events(items, RunEventKind.NOTICE) if "dropping" in n["text"]]
    assert len(dropped) == 1  # said once, not every turn after
    assert "WORKBENCH_INFERENCE_CONTEXT_TOKENS" in dropped[0]["text"]


def test_a_prompt_that_grows_as_expected_is_left_alone(monkeypatch, tmp_path):
    worktree = a_repository(tmp_path)
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(tool_call("read_file", {"path": "app.py"}), usage(1000)),
            sse(chunk(content="done"), usage(1100)),
        ),
    )

    items = drain(LocalBackend().run(a_request(phase=RunPhase.PLAN, worktree=worktree)))

    assert not [n for n in events(items, RunEventKind.NOTICE) if "dropping" in n["text"]]


def test_a_user_turn_in_between_is_not_mistaken_for_truncation():
    """Templates drop earlier reasoning at each new user turn, so the prompt
    legitimately shrinks across a nudge. Comparing across one would cry wolf
    at exactly the moment someone is reading the run."""
    window = backend_module._Window()
    messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
    window.check(messages, backend_module._Assistant(prompt_tokens=5000))
    window.sent = len(messages)
    messages += [
        {"role": "assistant", "content": "", "reasoning": "long thoughts " * 200},
        {"role": "user", "content": "keep going"},
    ]

    assert window.check(messages, backend_module._Assistant(prompt_tokens=2000)) is None


def test_a_server_that_reports_no_usage_is_never_accused():
    window = backend_module._Window()
    window.check([], backend_module._Assistant(prompt_tokens=None))

    assert (
        window.check([{"role": "tool", "content": "x" * 10_000}], backend_module._Assistant())
        is None
    )


def test_a_tool_call_the_server_could_not_parse_is_retried(monkeypatch, tmp_path):
    """Ollama reports a cut-off tool call as its own 500. One bad turn is the
    model's mistake to correct, not a reason to end the run."""
    worktree = a_repository(tmp_path)
    refused = httpx.Response(
        500,
        content=b'{"error":{"message":"error parsing tool call: unexpected end of JSON"}}',
    )
    replies = iter(
        [
            httpx.Response(200, content=sse(tool_call("read_file", {"path": "app.py"}))),
            refused,
            httpx.Response(
                200, content=sse(tool_call("submit_plan", {"plan": "P.", "subtasks": []}, "c2"))
            ),
        ]
    )

    def factory(base_url: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://model.test/v1",
            transport=httpx.MockTransport(lambda request: next(replies)),
        )

    monkeypatch.setattr(backend_module, "_client", factory)

    items = drain(LocalBackend().run(a_request(phase=RunPhase.PLAN, worktree=worktree)))

    assert isinstance(items[-1], AgentFinished)
    assert items[-1].text == "P."
    assert any("could not parse" in n["text"] for n in events(items, RunEventKind.NOTICE))


def test_any_other_server_error_mid_run_still_fails_it(monkeypatch, tmp_path):
    worktree = a_repository(tmp_path)
    replies = iter(
        [
            httpx.Response(200, content=sse(tool_call("read_file", {"path": "app.py"}))),
            httpx.Response(500, content=b'{"error":{"message":"out of memory"}}'),
        ]
    )

    def factory(base_url: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://model.test/v1",
            transport=httpx.MockTransport(lambda request: next(replies)),
        )

    monkeypatch.setattr(backend_module, "_client", factory)

    outcome = drain(LocalBackend().run(a_request(phase=RunPhase.PLAN, worktree=worktree)))[-1]

    assert isinstance(outcome, AgentFailed)
    assert "failed mid-run" in outcome.message


def test_an_execute_run_left_with_uncommitted_work_is_asked_to_finish(monkeypatch, tmp_path):
    """Run 66 ended with edits made, nothing committed, and no outcome — so
    nothing was published and nobody was told."""
    worktree = a_repository(tmp_path)
    monkeypatch.setattr(
        backend_module,
        "_client",
        stub(
            sse(tool_call("read_file", {"path": "app.py"})),
            sse(tool_call("write_file", {"path": "new.py", "content": "x = 2\n"}, "c2")),
            sse(chunk(content="I have made the change.")),
        ),
    )

    items = drain(LocalBackend().run(a_request(worktree=worktree)))

    notices = [n["text"] for n in events(items, RunEventKind.NOTICE)]
    assert any("uncommitted changes" in n for n in notices)


def test_a_prompt_a_few_tokens_under_the_floor_is_not_truncation():
    """Run 75: 16,754 read against a floor of 16,756, after a wall of pytest's
    dots — which tokenise far better than eight characters a token."""
    window = backend_module._Window()
    window.check([], backend_module._Assistant(prompt_tokens=16_000))
    window.sent = 0
    added = [{"role": "tool", "content": "." * 6_048}]  # floor: 16,000 + 756

    assert window.check(added, backend_module._Assistant(prompt_tokens=16_754)) is None
