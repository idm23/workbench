"""The Claude backend, exercised without a model.

Two things are worth pinning here. The translation from SDK messages into
`RunEventKind`, because that mapping is what makes an event log outlive the SDK
that wrote it — a regression would be invisible until someone read an old run.
And the permission mode, because getting it wrong produces a run that edits
files, cannot commit them, and burns its turn limit retrying.

`query` is stubbed throughout: no subprocess, no credential, no cost.
"""

import asyncio
import json
import subprocess
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
from workbench.agents.claude import ClaudeBackend, read_credential, translate
from workbench.agents.protocol import (
    CREDENTIAL_API_KEY,
    CREDENTIAL_NONE,
    CREDENTIAL_SUBSCRIPTION,
    CREDENTIAL_UNKNOWN,
    AgentEvent,
    AgentFailed,
    AgentFinished,
    AgentRequest,
    AgentUnavailable,
    SubtaskProposal,
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


# --- Calling back into Workbench --------------------------------------------


def test_the_run_and_task_ids_reach_the_environment(monkeypatch):
    from workbench.config import port

    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(run_id=42, task_id=7)))

    assert captured["options"].env == {
        "WORKBENCH_RUN_ID": "42",
        "WORKBENCH_TASK_ID": "7",
        "WORKBENCH_PROJECT_ID": "0",
        "WORKBENCH_API_BASE": f"http://127.0.0.1:{port()}",
    }


def test_the_project_id_reaches_the_environment_too(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(project_id=9)))

    assert captured["options"].env["WORKBENCH_PROJECT_ID"] == "9"


async def _drain_prompt(prompt) -> list[dict[str, Any]]:
    return [item async for item in prompt]


def test_no_input_channel_leaves_the_prompt_a_plain_string(monkeypatch):
    """Unchanged from before typed input existed at all — nothing wired up
    means byte-for-byte the same behaviour."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(prompt="Do the thing")))

    assert captured["prompt"] == "Do the thing"


def test_typed_input_wraps_the_prompt_as_a_lazy_stream(monkeypatch):
    async def one_more_message():
        yield "a follow-up"

    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(prompt="Do the thing", inputs=one_more_message())))

    assert not isinstance(captured["prompt"], str)
    sent = asyncio.run(_drain_prompt(captured["prompt"]))
    assert sent == [
        {"type": "user", "message": {"role": "user", "content": "Do the thing"}},
        {"type": "user", "message": {"role": "user", "content": "a follow-up"}},
    ]


def test_an_input_channel_with_nothing_new_still_sends_the_initial_prompt(monkeypatch):
    async def nothing_more():
        return
        yield  # pragma: no cover - makes this an async generator

    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(prompt="Do the thing", inputs=nothing_more())))

    sent = asyncio.run(_drain_prompt(captured["prompt"]))
    assert sent == [{"type": "user", "message": {"role": "user", "content": "Do the thing"}}]


def test_execute_loads_the_outcome_skill(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=RunPhase.EXECUTE)))

    assert captured["options"].plugins == [
        {"type": "local", "path": str(backend_module._PLUGIN_DIR)}
    ]
    assert captured["options"].skills == ["workbench-outcome"]


def test_plan_does_not_load_the_outcome_skill(monkeypatch):
    """It cannot call it anyway — plan mode runs no tools at all."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=RunPhase.PLAN)))

    assert captured["options"].plugins == []


def test_plan_sets_the_structured_output_schema(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=RunPhase.PLAN)))

    assert captured["options"].output_format == backend_module._PLAN_OUTPUT_FORMAT


def test_execute_does_not_set_a_structured_output_schema(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=RunPhase.EXECUTE)))

    assert captured["options"].output_format is None


def test_the_outcome_skill_files_exist():
    """A path handed to the SDK that silently does not exist would leave the
    agent with no skill and no error — nothing would say so."""
    plugin_json = backend_module._PLUGIN_DIR / ".claude-plugin" / "plugin.json"
    skill_md = backend_module._PLUGIN_DIR / "skills" / "workbench-outcome" / "SKILL.md"

    assert plugin_json.is_file()
    assert skill_md.is_file()
    assert "workbench-outcome" in skill_md.read_text()


def test_a_conversation_loads_the_tasks_skill_not_the_outcome_one(monkeypatch):
    """It has no single task to call finished or failed."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=RunPhase.CONVERSATION)))

    assert captured["options"].plugins == [
        {"type": "local", "path": str(backend_module._PLUGIN_DIR)}
    ]
    assert captured["options"].skills == ["workbench-tasks"]


def test_a_conversation_does_not_set_a_structured_output_schema(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=RunPhase.CONVERSATION)))

    assert captured["options"].output_format is None


def test_a_conversation_gets_its_own_generous_turn_limit(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=RunPhase.CONVERSATION)))

    assert captured["options"].max_turns == backend_module.MAX_TURNS_CONVERSATION
    assert backend_module.MAX_TURNS_CONVERSATION > backend_module.MAX_TURNS_EXECUTE


def test_a_conversation_runs_with_permissions_bypassed(monkeypatch):
    """The same reasoning as execute: managing the task list needs Bash."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()], captured))

    drain(ClaudeBackend().run(a_request(phase=RunPhase.CONVERSATION)))

    assert captured["options"].permission_mode == "bypassPermissions"


def test_the_tasks_skill_files_exist():
    skill_md = backend_module._PLUGIN_DIR / "skills" / "workbench-tasks" / "SKILL.md"

    assert skill_md.is_file()
    assert "workbench-tasks" in skill_md.read_text()


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


def test_a_plans_structured_plan_text_is_used_over_the_raw_result(monkeypatch):
    """`result.result` is the schema's JSON as text when output_format is set —
    showing that to a person would be unreadable."""
    monkeypatch.setattr(
        backend_module,
        "query",
        stub_query(
            [
                a_result(
                    result='{"plan": "do X", "subtasks": []}',
                    structured_output={"plan": "do X", "subtasks": []},
                )
            ]
        ),
    )

    outcome = drain(ClaudeBackend().run(a_request(phase=RunPhase.PLAN)))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.text == "do X"


def test_a_plans_proposed_subtasks_are_parsed(monkeypatch):
    monkeypatch.setattr(
        backend_module,
        "query",
        stub_query(
            [
                a_result(
                    structured_output={
                        "plan": "do X",
                        "subtasks": [
                            {"title": "a", "body": "b", "ready_to_execute": True},
                            {"title": "c", "body": "", "ready_to_execute": False},
                        ],
                    }
                )
            ]
        ),
    )

    outcome = drain(ClaudeBackend().run(a_request(phase=RunPhase.PLAN)))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.proposed_subtasks == [
        SubtaskProposal(title="a", body="b", ready_to_execute=True),
        SubtaskProposal(title="c", body="", ready_to_execute=False),
    ]


def test_an_execute_runs_structured_output_is_ignored(monkeypatch):
    """Execute never sets `output_format`, but stray data should still not
    leak into what gets shown or acted on."""
    monkeypatch.setattr(
        backend_module,
        "query",
        stub_query([a_result(result="the real summary", structured_output={"plan": "nope"})]),
    )

    outcome = drain(ClaudeBackend().run(a_request(phase=RunPhase.EXECUTE)))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.text == "the real summary"
    assert outcome.proposed_subtasks is None


def test_a_result_with_no_structured_output_proposes_nothing(monkeypatch):
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()]))

    outcome = drain(ClaudeBackend().run(a_request(phase=RunPhase.PLAN)))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.proposed_subtasks is None
    assert outcome.text == "Here is what I did."  # falls back to result.result


def test_a_cut_short_result_is_marked_stopped_early(monkeypatch):
    monkeypatch.setattr(
        backend_module, "query", stub_query([a_result(terminal_reason="aborted_tools")])
    )

    outcome = drain(ClaudeBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.stopped_early is True


def test_a_cleanly_completed_result_is_not_stopped_early(monkeypatch):
    monkeypatch.setattr(
        backend_module, "query", stub_query([a_result(terminal_reason="completed")])
    )

    outcome = drain(ClaudeBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.stopped_early is False


def test_an_older_sdk_with_no_terminal_reason_is_not_stopped_early(monkeypatch):
    """The field might not exist on an older SDK version — absence must not
    read as a problem."""
    monkeypatch.setattr(backend_module, "query", stub_query([a_result()]))

    outcome = drain(ClaudeBackend().run(a_request()))[-1]

    assert isinstance(outcome, AgentFinished)
    assert outcome.stopped_early is False


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


# --- The credential probe -----------------------------------------------------
#
# Every payload below is a real recording of `claude auth status --json` rather
# than a guess at its shape, because the whole value of this check is that it
# distinguishes cases nobody would think to invent — notably an API key, which
# reports itself as a perfectly good login.

SIGNED_IN = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "email": "someone@example.com",
    "orgName": "someone@example.com's Organization",
    "subscriptionType": "pro",
}
SIGNED_OUT = {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}
API_KEY = {
    "loggedIn": True,
    "authMethod": "api_key",
    "apiProvider": "firstParty",
    "apiKeySource": "ANTHROPIC_API_KEY",
}


def test_a_subscription_login_is_what_workbench_wants():
    status = read_credential(SIGNED_IN)

    assert status.logged_in
    assert status.method == CREDENTIAL_SUBSCRIPTION
    assert status.account == "someone@example.com"


def test_being_signed_out_is_reported_rather_than_raised():
    status = read_credential(SIGNED_OUT)

    assert not status.logged_in
    assert status.method == CREDENTIAL_NONE
    assert status.detail


def test_an_api_key_under_subscription_billing_is_not_authenticated():
    """The failure this check exists for.

    `loggedIn` is true and the CLI is perfectly happy; every run would simply
    bill per token instead of the subscription, and nothing visible in
    Workbench would change until an invoice arrived.
    """
    status = read_credential(API_KEY)

    assert not status.logged_in
    assert status.method == CREDENTIAL_API_KEY
    # Naming both the culprit and the way to opt in on purpose, because the
    # person reading this has no other way to tell which of the two it is.
    assert "ANTHROPIC_API_KEY" in status.detail
    assert "WORKBENCH_BILLING=api" in status.detail


def test_an_api_key_is_correct_when_metered_billing_was_chosen(monkeypatch):
    monkeypatch.setenv("WORKBENCH_BILLING", "api")

    status = read_credential(API_KEY)

    assert status.logged_in
    assert status.method == CREDENTIAL_API_KEY


def test_an_unrecognised_method_is_unknown_rather_than_a_verdict():
    status = read_credential({"loggedIn": True, "authMethod": "bedrock"})

    assert status.method == CREDENTIAL_UNKNOWN
    assert not status.logged_in
    assert "bedrock" in status.detail


class FakeProbe:
    """Stands in for the CLI. Records the environment it was handed, which is
    the only way to assert on the thing that matters most here."""

    def __init__(self, stdout: str = "", returncode: int = 0, raises: Exception | None = None):
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.env: dict[str, str] = {}
        self.argv: list[str] = []

    def __call__(self, argv, **kwargs):
        self.argv = argv
        self.env = kwargs.get("env") or {}
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


@pytest.fixture
def probe(monkeypatch):
    """A located CLI whose answer the test chooses."""

    def install(**kwargs) -> FakeProbe:
        fake = FakeProbe(**kwargs)
        monkeypatch.setattr(backend_module, "_cli_path", lambda: "/nonexistent/claude")
        monkeypatch.setattr(backend_module.subprocess, "run", fake)
        return fake

    return install


def test_the_probe_sees_what_the_runner_will_see(probe, monkeypatch):
    """Run under `agent_environment()`, not the ambient environment.

    A key exported in the shell that asked is stripped before a run starts, so
    reporting it here would be worse than not checking at all — it would say
    "authenticated" about a credential the runner removes.
    """
    monkeypatch.delenv("WORKBENCH_BILLING", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-really")
    fake = probe(stdout=json.dumps(SIGNED_IN))

    ClaudeBackend().credential_status()

    assert "ANTHROPIC_API_KEY" not in fake.env


def test_a_logged_out_cli_answers_one_and_is_still_read(probe):
    """`auth status` exits 1 when signed out but still prints a good report.

    Treating a non-zero exit as unreadable would turn the single most important
    case into `unknown`, which is the one verdict that raises no warning.
    """
    probe(stdout=json.dumps(SIGNED_OUT), returncode=1)

    status = ClaudeBackend().credential_status()

    assert status.method == CREDENTIAL_NONE


def test_a_missing_cli_is_unknown(monkeypatch):
    monkeypatch.setattr(backend_module, "_cli_path", lambda: None)

    status = ClaudeBackend().credential_status()

    assert status.method == CREDENTIAL_UNKNOWN
    assert not status.logged_in


def test_a_wedged_cli_is_unknown_rather_than_a_hang(probe):
    probe(raises=subprocess.TimeoutExpired(cmd="claude", timeout=20))

    status = ClaudeBackend().credential_status()

    assert status.method == CREDENTIAL_UNKNOWN


def test_an_unrunnable_cli_is_unknown(probe):
    probe(raises=OSError("Permission denied"))

    status = ClaudeBackend().credential_status()

    assert status.method == CREDENTIAL_UNKNOWN


@pytest.mark.parametrize("output", ["", "not json at all", "[1, 2, 3]", "null"])
def test_unreadable_output_is_unknown_and_never_raises(probe, output):
    probe(stdout=output)

    status = ClaudeBackend().credential_status()

    assert status.method == CREDENTIAL_UNKNOWN


def test_the_probe_asks_for_json(probe):
    """--json happens to be the CLI's default. Passing it anyway, because a
    default that changes turns this into the unreadable-output case above."""
    fake = probe(stdout=json.dumps(SIGNED_IN))

    ClaudeBackend().credential_status()

    assert fake.argv[1:] == ["auth", "status", "--json"]


def test_a_backend_says_how_to_sign_itself_in():
    """The fix is carried by the backend, not composed by whoever reports it.

    A doctor that spelled out `claude auth login` would be a second place that
    knows which vendor answered, which is the decay the seam exists to stop.
    """
    status = read_credential(SIGNED_OUT, cli="/opt/claude")

    assert status.login_command == ("/opt/claude", "auth", "login", "--claudeai")


def test_the_login_command_never_offers_the_metered_branch():
    """`--console` authenticates perfectly and bills per token. An instruction
    someone could follow into it is the failure this check exists to catch."""
    status = read_credential(SIGNED_OUT, cli="/opt/claude")

    assert "--console" not in status.login_command


def test_there_is_no_login_command_when_there_is_no_cli():
    status = read_credential(SIGNED_OUT)

    assert status.login_command == ()
