"""The detached runner.

What matters here is not that a happy run works, but that every way a run can
end still ends in a recorded outcome. A run left in `running` with a dead pid
holds a concurrency slot forever and explains nothing to whoever finds it, so
each failure path gets its own test.
"""

import asyncio
import os
import signal

import pytest

from workbench.agents.protocol import (
    AgentEvent,
    AgentFailed,
    AgentFinished,
    AgentRequest,
    AgentUnavailable,
)
from workbench.agents.registry import UnknownBackend
from workbench.agents.tests.fake import FakeBackend
from workbench.database.models import RunEvent, RunEventKind, RunPhase, RunStatus
from workbench.runs import runner as runner_module
from workbench.runs.runner import Interrupted, NotPrepared, execute, main, prepare, resume_token_for
from workbench.runs.store import create_run, finish_run


@pytest.fixture
def backend(monkeypatch):
    """Install a scripted backend in place of whatever the registry would give."""
    fake = FakeBackend(
        events=[
            AgentEvent(RunEventKind.TEXT, {"text": "Looking at the code."}),
            AgentEvent(RunEventKind.TOOL_USE, {"name": "Bash", "input": {"command": "ls"}}),
        ],
        outcome=AgentFinished(
            text="Added the tests.",
            resume_token="session-abc",
            model="claude-x",
            total_cost_usd=0.31,
            num_turns=7,
        ),
    )
    monkeypatch.setattr(runner_module, "get_backend", lambda _name: fake)
    return fake


def events_for(db, run):
    return db.query(RunEvent).filter_by(run_id=run.id).order_by(RunEvent.seq).all()


# --- The happy path --------------------------------------------------------


def test_a_finished_execute_run_succeeds_with_its_summary(db, run, checkout, backend):
    execute(db, run)

    assert run.status is RunStatus.SUCCEEDED
    assert run.summary == "Added the tests."


def test_usage_and_the_resume_token_are_recorded(db, run, checkout, backend):
    execute(db, run)

    assert run.resume_token == "session-abc"
    assert run.model == "claude-x"
    assert run.total_cost_usd == 0.31
    assert run.num_turns == 7


def test_every_agent_event_reaches_the_log_in_order(db, run, checkout, backend):
    execute(db, run)

    kinds = [e.kind for e in events_for(db, run)]
    assert RunEventKind.TEXT in kinds
    assert RunEventKind.TOOL_USE in kinds
    assert kinds.index(RunEventKind.TEXT) < kinds.index(RunEventKind.TOOL_USE)


def test_the_log_opens_and_closes_with_a_status(db, run, checkout, backend):
    """So a reader can tell a run that never started from one still going."""
    execute(db, run)

    events = events_for(db, run)
    assert events[0].kind is RunEventKind.STATUS
    assert events[0].payload == {"status": "running"}
    assert events[-1].payload == {"status": "succeeded"}


def test_sequence_numbers_are_dense_and_ordered(db, run, checkout, backend):
    """Replay from Last-Event-ID depends on this, so it is worth pinning."""
    execute(db, run)

    seqs = [e.seq for e in events_for(db, run)]
    assert seqs == list(range(1, len(seqs) + 1))


def test_the_worktree_is_recorded_on_the_task(db, run, checkout, backend):
    """On the task, because the execute run after a plan run reuses it."""
    execute(db, run)

    assert run.task.branch == "workbench/task-1-add-route-tests"
    assert run.task.worktree_path is not None


def test_the_agent_is_pointed_at_that_worktree(db, run, checkout, backend):
    execute(db, run)

    assert str(backend.requests[0].worktree) == run.task.worktree_path


def test_the_handle_is_cleared_once_the_run_is_over(db, run, checkout, backend):
    execute(db, run)

    assert run.handle is None
    statuses = [e for e in events_for(db, run) if e.kind is RunEventKind.STATUS]
    assert statuses[0].payload["status"] == "running"


# --- The plan phase --------------------------------------------------------


def test_a_plan_run_stops_for_a_person(db, task, checkout, backend):
    run = create_run(db, task, RunPhase.PLAN, backend="fake")

    execute(db, run)

    assert run.status is RunStatus.AWAITING_REVIEW
    assert run.plan == "Added the tests."
    assert run.summary is None


def test_the_plan_phase_asks_the_agent_not_to_change_anything(db, task, checkout, backend):
    run = create_run(db, task, RunPhase.PLAN, backend="fake")

    execute(db, run)

    assert "Do not make any changes yet" in backend.requests[0].prompt.replace("\n", " ")


# --- Resuming --------------------------------------------------------------


def test_an_execute_run_resumes_the_plan_that_preceded_it(db, task, checkout, backend):
    plan = create_run(db, task, RunPhase.PLAN, backend="fake")
    finish_run(db, plan, RunStatus.AWAITING_REVIEW, plan="p", resume_token="session-from-plan")
    execute_run = create_run(db, task, RunPhase.EXECUTE, backend="fake")

    execute(db, execute_run)

    assert backend.requests[0].resume_token == "session-from-plan"


def test_a_token_from_another_backend_is_never_reused(db, task):
    """It is opaque, and means nothing to the backend that did not issue it."""
    other = create_run(db, task, RunPhase.PLAN, backend="something-else")
    finish_run(db, other, RunStatus.AWAITING_REVIEW, resume_token="not-ours")

    assert resume_token_for(db, task, "fake") is None


def test_the_most_recent_token_wins(db, task):
    first = create_run(db, task, RunPhase.PLAN, backend="fake")
    finish_run(db, first, RunStatus.FAILED, resume_token="older")
    second = create_run(db, task, RunPhase.PLAN, backend="fake")
    finish_run(db, second, RunStatus.AWAITING_REVIEW, resume_token="newer")

    assert resume_token_for(db, task, "fake") == "newer"


def test_a_first_run_starts_cold(db, task, checkout, backend):
    run = create_run(db, task, RunPhase.PLAN, backend="fake")

    execute(db, run)

    assert backend.requests[0].resume_token is None


# --- Every way it can go wrong ---------------------------------------------


def test_an_uncloned_project_fails_before_anything_is_attempted(db, run, backend):
    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert "cloned" in (run.error or "")
    assert backend.requests == []


def test_an_unknown_backend_name_fails_with_the_names_that_exist(db, run, checkout, monkeypatch):
    monkeypatch.setattr(
        runner_module, "get_backend", lambda name: UnknownBackend(name, ("claude",))
    )

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert "claude" in (run.error or "")


def test_a_failing_setup_command_stops_the_run(db, run, checkout, backend):
    run.task.project.setup_command = "exit 3"
    db.commit()

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert "Setup command failed" in (run.error or "")
    assert backend.requests == []


def test_an_agent_failure_keeps_the_usage_it_reported(db, run, checkout, monkeypatch):
    monkeypatch.setattr(
        runner_module,
        "get_backend",
        lambda _n: FakeBackend(
            outcome=AgentFailed("ran out of turns", resume_token="s1", total_cost_usd=0.9)
        ),
    )

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert run.error == "ran out of turns"
    assert run.total_cost_usd == 0.9
    assert run.resume_token == "s1"


def test_an_unavailable_agent_is_a_failure_with_no_diffstat(db, run, checkout, monkeypatch):
    """Nothing ran, so there is nothing to diff and nothing to summarise."""
    monkeypatch.setattr(
        runner_module, "get_backend", lambda _n: FakeBackend(outcome=AgentUnavailable("no CLI"))
    )

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert run.error == "no CLI"
    assert run.diffstat is None


def test_a_backend_that_yields_no_outcome_still_ends_the_run(db, run, checkout, monkeypatch):
    class Silent:
        name = "silent"

        async def run(self, request: AgentRequest):
            yield AgentEvent(RunEventKind.TEXT, {"text": "hi"})

    monkeypatch.setattr(runner_module, "get_backend", lambda _n: Silent())

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert "no outcome" in (run.error or "")


def test_a_backend_that_raises_does_not_leave_the_run_running(db, run, checkout, monkeypatch):
    """The protocol says it should not. The runner cannot rely on that."""

    class Exploding:
        name = "exploding"

        async def run(self, request: AgentRequest):
            raise RuntimeError("unexpected")
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(runner_module, "get_backend", lambda _n: Exploding())

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert "crashed" in (run.error or "")
    assert run.finished_at is not None


def test_a_run_is_never_left_running_whatever_happens(db, task, checkout, monkeypatch):
    """The property the individual cases above are each an instance of."""
    outcomes = [
        AgentFinished(text="ok"),
        AgentFailed("nope"),
        AgentUnavailable("gone"),
    ]
    for outcome in outcomes:
        monkeypatch.setattr(
            runner_module, "get_backend", lambda _n, o=outcome: FakeBackend(outcome=o)
        )
        run = create_run(db, task, RunPhase.EXECUTE, backend="fake")

        execute(db, run)

        assert run.status is not RunStatus.RUNNING
        assert run.handle is None
        assert run.finished_at is not None


# --- Interruption ----------------------------------------------------------


def test_a_signalled_run_is_cancelled_rather_than_lost(db, run, checkout, monkeypatch):
    """A deploy restarting the service is the ordinary cause of this."""

    class Slow:
        name = "slow"

        async def run(self, request: AgentRequest):
            yield AgentEvent(RunEventKind.TEXT, {"text": "starting"})
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(5)
            yield AgentFinished(text="never reached")

    monkeypatch.setattr(runner_module, "get_backend", lambda _n: Slow())

    execute(db, run)

    assert run.status is RunStatus.CANCELLED
    assert "SIGTERM" in (run.error or "")


def test_work_done_before_the_signal_is_still_in_the_log(db, run, checkout, monkeypatch):
    class Slow:
        name = "slow"

        async def run(self, request: AgentRequest):
            yield AgentEvent(RunEventKind.TEXT, {"text": "starting"})
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(5)
            yield AgentFinished(text="never reached")

    monkeypatch.setattr(runner_module, "get_backend", lambda _n: Slow())

    execute(db, run)

    texts = [e.payload.get("text") for e in events_for(db, run)]
    assert "starting" in texts


def test_the_default_signal_disposition_is_restored(db, run, checkout, backend):
    """A handler left installed would outlive the run inside a test session."""
    before = signal.getsignal(signal.SIGTERM)

    execute(db, run)

    assert signal.getsignal(signal.SIGTERM) == before


# --- prepare() in isolation ------------------------------------------------


def test_prepare_reports_the_project_that_is_missing(db, run):
    result = prepare(db, run)

    assert isinstance(result, NotPrepared)
    assert "idm23/workbench" in result.message


def test_prepare_notes_each_slow_step_in_the_log(db, run, checkout, backend):
    """A first clone or a setup command is slow enough to look like a hang."""
    prepare(db, run)

    notices = [e.payload["text"] for e in events_for(db, run) if e.kind is RunEventKind.NOTICE]
    assert any("worktree" in text for text in notices)


# --- Choosing what to branch from -------------------------------------------


def test_prepare_rejects_an_origin_that_no_longer_resolves(db, run, checkout):
    """Defense in depth: the web route already validates this at request time."""
    run.task.origin_ref = "task:9999"
    db.commit()

    result = prepare(db, run)

    assert isinstance(result, NotPrepared)
    assert "not a valid origin" in result.message


def test_prepare_branches_from_the_chosen_origin(db, run, checkout):
    run.task.origin_ref = "staging"
    db.commit()

    prepare(db, run)

    notices = [e.payload["text"] for e in events_for(db, run) if e.kind is RunEventKind.NOTICE]
    assert any("from staging" in text for text in notices)


def test_prepare_fetches_before_creating_a_new_worktree(db, run, checkout, monkeypatch):
    """The fix for the actual trap: a clone that is never fetched again."""
    real_fetch = runner_module.fetch_checkout
    calls = []
    monkeypatch.setattr(
        runner_module,
        "fetch_checkout",
        lambda repo: calls.append(repo) or real_fetch(repo),
    )

    prepare(db, run)

    assert calls == [checkout]


def test_prepare_does_not_refetch_once_a_worktree_already_exists(
    db, run, checkout, backend, monkeypatch
):
    """Re-fetching a large repository on every execute run would cost time
    for no benefit — the branch is already fixed by then."""
    execute(db, run)
    assert run.task.worktree_path is not None

    calls = []
    monkeypatch.setattr(runner_module, "fetch_checkout", lambda repo: calls.append(repo))
    second = create_run(db, run.task, RunPhase.EXECUTE, backend="fake")

    prepare(db, second)

    assert calls == []


# --- The entry point -------------------------------------------------------


def test_main_refuses_a_run_that_is_already_going(db, run, checkout, backend):
    """Two processes on one run would interleave events and both write an outcome."""
    run.status = RunStatus.RUNNING
    db.commit()

    assert main([str(run.id)]) == 1


def test_main_refuses_an_id_that_does_not_exist(data_dir, db):
    assert main(["9999"]) == 1


def test_main_rejects_a_missing_or_malformed_argument(data_dir):
    assert main([]) == 2
    assert main(["not-a-number"]) == 2


def test_main_strips_metered_api_credentials(db, run, checkout, backend, monkeypatch):
    """The subscription decision, enforced at the one point every backend inherits."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-survive")

    main([str(run.id)])

    assert "ANTHROPIC_API_KEY" not in os.environ


def test_main_keeps_the_key_when_api_billing_is_asked_for(db, run, checkout, backend, monkeypatch):
    """Opting into metered billing has to be possible, just never accidental."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-deliberate")
    monkeypatch.setenv("WORKBENCH_BILLING", "api")

    main([str(run.id)])

    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-deliberate"


def test_main_runs_a_queued_run_to_completion(db, run, checkout, backend):
    assert main([str(run.id)]) == 0

    db.refresh(run)
    assert run.status is RunStatus.SUCCEEDED


def test_record_translates_an_interruption_without_a_worktree(db, run):
    """A run signalled before `prepare` finished has no diff to take."""
    runner_module.record(db, run, Interrupted("SIGTERM"))

    assert run.status is RunStatus.CANCELLED
    assert run.diffstat is None


def test_main_leaves_the_rest_of_the_environment_intact(db, run, checkout, backend, monkeypatch):
    """Stripping credentials must not become sanitising everything.

    Under a subscription the credential is found through HOME, so a runner that
    cleared its own environment would authenticate as nobody — and would fail
    in a way that looks like a missing login rather than a bug in Workbench.
    """
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    main([str(run.id)])

    assert os.environ["HOME"] == "/home/someone"
    assert os.environ["PATH"] == "/usr/bin:/bin"
