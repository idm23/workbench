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

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://model.test/v1", transport=httpx.MockTransport(handler)
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
    assert [message["content"] for message in resumed[-3:]] == [
        "Do the thing",
        "first",
        "And now this",
    ]


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
