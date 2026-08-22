"""The Claude backend, exercised without a model.

Two things are worth pinning here. The translation from SDK messages into
`RunEventKind`, because that mapping is what makes an event log outlive the SDK
that wrote it — a regression would be invisible until someone read an old run.
And the permission mode, because getting it wrong produces a run that edits
files, cannot commit them, and burns its turn limit retrying.

`query` is stubbed throughout: no subprocess, no credential, no cost.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from workbench.agents import claude as backend_module
from workbench.agents.claude import ClaudeBackend, translate
from workbench.agents.protocol import (
    AgentEvent,
    AgentFailed,
    AgentFinished,
    AgentRequest,
    AgentUnavailable,
)
from workbench.agents.tests.helpers import drain
from workbench.database.models import RunEventKind, RunPhase


def a_result(**overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1000,
        "duration_api_ms": 900,
        "is_error": False,
        "num_turns": 4,
        "session_id": "session-abc",
        "stop_reason": None,
        "total_cost_usd": 0.42,
        "usage": None,
        "result": "Here is what I did.",
    }
    return ResultMessage(**(fields | overrides))


def a_request(**overrides: Any) -> AgentRequest:
    fields: dict[str, Any] = {
        "worktree": Path("/tmp/worktree"),
        "phase": RunPhase.EXECUTE,
        "prompt": "Do the thing",
    }
    return AgentRequest(**(fields | overrides))


def stub_query(messages: list[Any], captured: dict[str, Any] | None = None):
    """Replace the SDK's `query` with a fixed sequence of messages."""

    async def fake_query(*, prompt: str, options: Any = None, **_: Any) -> AsyncIterator[Any]:
        if captured is not None:
            captured["prompt"] = prompt
            captured["options"] = options
        for message in messages:
            yield message

    return fake_query


# --- Translation -----------------------------------------------------------


def test_prose_becomes_a_text_event():
    events = translate(AssistantMessage(content=[TextBlock(text="Hello")], model="m"))

    assert [e.kind for e in events] == [RunEventKind.TEXT]
    assert events[0].payload["text"] == "Hello"


def test_reasoning_is_kept_separate_from_prose():
    """The reader renders them differently, which is why the vocabulary splits them."""
    events = translate(
        AssistantMessage(content=[ThinkingBlock(thinking="hmm", signature="s")], model="m")
    )

    assert [e.kind for e in events] == [RunEventKind.THINKING]


def test_empty_prose_is_not_recorded():
    """Whitespace-only blocks are common and would clutter a run for nothing."""
    events = translate(AssistantMessage(content=[TextBlock(text="   ")], model="m"))

    assert events == []


def test_a_tool_call_keeps_its_name_and_input():
    events = translate(
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})], model="m"
        )
    )

    assert events[0].kind is RunEventKind.TOOL_USE
    assert events[0].payload["name"] == "Bash"
    assert events[0].payload["input"]["command"] == "ls"


def test_a_tool_result_arrives_as_a_user_message():
    events = translate(UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="out")]))

    assert [e.kind for e in events] == [RunEventKind.TOOL_RESULT]
    assert events[0].payload["text"] == "out"
    assert events[0].payload["is_error"] is False


def test_a_failed_tool_says_so():
    events = translate(
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="boom", is_error=True)])
    )

    assert events[0].payload["is_error"] is True


def test_our_own_prompt_is_not_echoed_into_the_log():
    assert translate(UserMessage(content="Do the thing")) == []


def test_setup_chatter_is_dropped():
    assert translate(SystemMessage(subtype="init", data={"tools": []})) == []
    assert translate(SystemMessage(subtype="thinking_tokens", data={})) == []


def test_other_system_messages_become_notices():
    events = translate(SystemMessage(subtype="compact_boundary", data={"trigger": "auto"}))

    assert [e.kind for e in events] == [RunEventKind.NOTICE]


def test_an_unrecognised_message_becomes_a_notice_not_a_crash():
    """The vocabulary grows when a reader needs it, not when an SDK adds a class."""
    events = translate(object())

    assert [e.kind for e in events] == [RunEventKind.NOTICE]


def test_a_backend_error_is_surfaced_alongside_the_content():
    events = translate(
        AssistantMessage(content=[TextBlock(text="hi")], model="m", error="rate_limit")
    )

    assert [e.kind for e in events] == [RunEventKind.TEXT, RunEventKind.NOTICE]
    assert "rate_limit" in events[1].payload["text"]


def test_the_result_message_is_not_an_event():
    """It becomes the outcome; recording it twice would double-count the summary."""
    assert translate(a_result()) == []


# --- Payload bounds --------------------------------------------------------


def test_a_huge_tool_input_is_clipped():
    """One Write call can carry a whole file, and every event is a row kept forever."""
    events = translate(
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Write", input={"content": "x" * 50_000})],
            model="m",
        )
    )

    clipped = events[0].payload["input"]["content"]
    assert len(clipped) < 50_000
    assert "truncated" in clipped


def test_clipping_keeps_the_small_keys_readable():
    """Per value, not per blob, so the path survives when the content does not."""
    events = translate(
        AssistantMessage(
            content=[
                ToolUseBlock(id="t1", name="Write", input={"path": "a.py", "content": "y" * 50_000})
            ],
            model="m",
        )
    )

    assert events[0].payload["input"]["path"] == "a.py"


def test_a_short_input_is_left_exactly_alone():
    events = translate(
        AssistantMessage(content=[ToolUseBlock(id="t", name="Read", input={"p": "a"})], model="m")
    )

    assert events[0].payload["input"] == {"p": "a"}


def test_payloads_are_json_serialisable():
    """They go into a JSON column and out to a browser verbatim."""
    events = translate(
        AssistantMessage(
            content=[
                TextBlock(text="hi"),
                ToolUseBlock(id="t", name="Bash", input={"command": "ls", "timeout": 5}),
            ],
            model="m",
        )
    )

    for event in events:
        json.dumps(event.payload)


def test_structured_tool_results_are_flattened():
    events = translate(
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="t1",
                    content=[{"type": "text", "text": "line"}, {"type": "image"}],
                )
            ]
        )
    )

    assert events[0].payload["text"] == "line\n[image]"


# --- Permission mode, the finding behind task 6 ----------------------------


def test_the_execute_phase_can_actually_commit(monkeypatch):
    """`acceptEdits` permits edits but gates Bash, so the agent cannot commit.

    Found by burning 33 turns watching a run retry `git commit` and fail.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=RunPhase.EXECUTE)))

    assert captured["options"].permission_mode == "bypassPermissions"


def test_the_plan_phase_is_held_read_only(monkeypatch):
    """Read-only is the product of a plan run, so enforcement and intent agree."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=RunPhase.PLAN)))

    assert captured["options"].permission_mode == "plan"


def test_the_agent_runs_in_the_tasks_worktree(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(worktree=Path("/srv/worktrees/task-7"))))

    assert captured["options"].cwd == Path("/srv/worktrees/task-7")


def test_a_resume_token_is_passed_through_unparsed(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(resume_token="session-abc")))

    assert captured["options"].resume == "session-abc"


def test_each_phase_gets_its_own_turn_limit(monkeypatch):
    """Nobody is watching a detached run spend money."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=RunPhase.PLAN)))
    plan_turns = captured["options"].max_turns
    drain(ClaudeBackend().run(a_request(phase=RunPhase.EXECUTE)))

    assert plan_turns < captured["options"].max_turns


# --- Outcomes --------------------------------------------------------------


def test_a_completed_run_reports_its_text_token_and_usage(monkeypatch):
    monkeypatch.setattr(
        backend_module,
        "query",
        stub_query(
            [AssistantMessage(content=[TextBlock(text="hi")], model="claude-x"), a_result()]
        ),
    )

    yielded = drain(ClaudeBackend().run(a_request()))

    assert isinstance(yielded[0], AgentEvent)
    outcome = yielded[-1]
    assert isinstance(outcome, AgentFinished)
    assert outcome.text == "Here is what I did."
    assert outcome.resume_token == "session-abc"
    assert outcome.total_cost_usd == 0.42
    assert outcome.num_turns == 4


def test_the_model_recorded_is_the_one_that_answered(monkeypatch):
    """`request.model` is a preference; the run records what actually ran."""
    monkeypatch.setattr(
        backend_module,
        "query",
        stub_query(
            [AssistantMessage(content=[TextBlock(text="hi")], model="claude-actual"), a_result()]
        ),
    )

    outcome = drain(ClaudeBackend().run(a_request(model="claude-requested")))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.model == "claude-actual"


def test_the_outcome_is_always_last(monkeypatch):
    monkeypatch.setattr(
        backend_module,
        "query",
        stub_query([AssistantMessage(content=[TextBlock(text="a")], model="m"), a_result()]),
    )

    yielded = drain(ClaudeBackend().run(a_request()))

    assert all(isinstance(item, AgentEvent) for item in yielded[:-1])
    assert not isinstance(yielded[-1], AgentEvent)


def test_an_error_result_is_a_failure_that_keeps_its_usage(monkeypatch):
    """Work may already be committed, so the cost still has to be recorded."""
    monkeypatch.setattr(
        backend_module, "query", stub_query([a_result(is_error=True, result="ran out of turns")])
    )

    outcome = drain(ClaudeBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentFailed)
    assert outcome.message == "ran out of turns"
    assert outcome.total_cost_usd == 0.42
    assert outcome.resume_token == "session-abc"


def test_a_missing_cli_means_nothing_was_attempted(monkeypatch):
    def raising_query(**_: Any):
        raise CLINotFoundError("no binary")

    monkeypatch.setattr(backend_module, "query", raising_query)

    outcome = drain(ClaudeBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentUnavailable)


def test_a_crashed_subprocess_is_a_failure_not_unavailability(monkeypatch):
    """It may have committed before dying, so the worktree is not untouched."""

    async def raising_query(**_: Any):
        raise ProcessError("died")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(backend_module, "query", raising_query)

    outcome = drain(ClaudeBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentFailed)


def test_a_stream_that_ends_without_a_result_is_a_failure(monkeypatch):
    monkeypatch.setattr(
        backend_module,
        "query",
        stub_query([AssistantMessage(content=[TextBlock(text="hi")], model="m")]),
    )

    outcome = drain(ClaudeBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentFailed)
    assert "without reporting a result" in outcome.message


def test_the_backend_never_raises_for_an_ordinary_failure(monkeypatch):
    """A traceback out of a detached runner helps nobody."""

    def raising_query(**_: Any):
        raise CLINotFoundError("no binary")

    monkeypatch.setattr(backend_module, "query", raising_query)

    drain(ClaudeBackend().run(a_request()))


@pytest.mark.parametrize("phase", list(RunPhase))
def test_every_phase_has_a_permission_mode_and_a_turn_limit(phase, monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=phase)))

    assert captured["options"].permission_mode
    assert captured["options"].max_turns
