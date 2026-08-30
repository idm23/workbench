"""Starting, cancelling, and reaping runs.

Three properties matter here and each is easy to lose quietly. A run may not
start if the machine is already busy, because what two extra agents waste is a
rate-limit window shared with everything else on the account. A run that is
executing must always be reachable, or it cannot be stopped. And a run whose
job has died must eventually stop looking active, or it holds a slot against
the cap forever and the cap becomes a way to lock yourself out.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from workbench.config import run_unit_name
from workbench.database.models import Run, RunEvent, RunPhase, RunStatus
from workbench.runs import lifecycle
from workbench.runs.executors import (
    Started,
    StartRefused,
    SystemdUnitExecutor,
    UnknownExecutor,
)
from workbench.runs.lifecycle import (
    AlreadyRunning,
    NotCancellable,
    NotStarted,
    TooManyRuns,
    cancel_run,
    reap,
    start_conversation,
    start_run,
)
from workbench.runs.store import create_run, finish_run, record_launch


@dataclass
class FakeExecutor:
    """An executor that starts nothing and remembers being asked."""

    name: str = "fake"
    alive: bool = True
    refuses: str | None = None
    started: list[int] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    def start(self, run_id: int):
        if self.refuses:
            return StartRefused(self.refuses)
        self.started.append(run_id)
        return Started(f"fake-{run_id}")

    def cancel(self, handle: str) -> bool:
        self.cancelled.append(handle)
        return self.alive

    def is_running(self, handle: str) -> bool:
        return self.alive


@pytest.fixture
def executor(monkeypatch):
    fake = FakeExecutor()
    monkeypatch.setattr(lifecycle, "get_executor", lambda _name=None: fake)
    return fake


@pytest.fixture(autouse=True)
def generous_cap(monkeypatch):
    monkeypatch.setenv("WORKBENCH_MAX_CONCURRENT_RUNS", "2")


def age(db, run: Run, seconds: int) -> Run:
    """Backdate a run so reaping is willing to judge it."""
    run.created_at = datetime.now(UTC) - timedelta(seconds=seconds)
    db.commit()
    return run


# --- Starting --------------------------------------------------------------


def test_starting_a_run_records_how_it_was_started(db, task, executor):
    run = start_run(db, task, RunPhase.PLAN)

    assert isinstance(run, Run)
    assert run.executor == "fake"
    assert run.handle == "fake-1"
    assert executor.started == [run.id]


def test_the_handle_is_recorded_before_the_job_is_asked_to_run(db, task, monkeypatch):
    """Otherwise a crash in between leaves something running and unreachable.

    Checked against the systemd executor specifically, because it is the one
    whose handle is knowable in advance — the unit name comes from the run id,
    so it can be written before the unit exists. A pid cannot be, which is one
    more reason units are the better answer.
    """
    seen: list[str | None] = []

    class WatchfulSystemd(SystemdUnitExecutor):
        def start(self, run_id: int):
            seen.append(db.get(Run, run_id).handle)
            return Started(run_unit_name(run_id))

    monkeypatch.setattr(lifecycle, "get_executor", lambda _name=None: WatchfulSystemd())

    run = start_run(db, task, RunPhase.PLAN)

    assert isinstance(run, Run)
    assert seen == [run_unit_name(run.id)]
    assert run.executor == "systemd-unit"


def test_the_run_takes_the_projects_backend(db, task, executor):
    task.project.agent_backend = "something-else"
    db.commit()

    run = start_run(db, task, RunPhase.PLAN)

    assert isinstance(run, Run)
    assert run.backend == "something-else"


def test_the_queue_transition_is_in_the_log(db, task, executor):
    run = start_run(db, task, RunPhase.PLAN)

    assert isinstance(run, Run)
    events = db.query(RunEvent).filter_by(run_id=run.id).all()
    assert events[0].payload["executor"] == "fake"


# --- Starting a conversation -------------------------------------------------


def test_starting_a_conversation_records_how_it_was_started(db, task, executor):
    run = start_conversation(db, task.project)

    assert isinstance(run, Run)
    assert run.phase is RunPhase.CONVERSATION
    assert run.task_id is None
    assert run.project_id == task.project.id
    assert run.executor == "fake"
    assert executor.started == [run.id]


def test_a_second_conversation_reports_the_first_rather_than_duplicating(db, task, executor):
    """Clicking the project again should resume it, not start a rival one."""
    first = start_conversation(db, task.project)
    assert isinstance(first, Run)

    second = start_conversation(db, task.project)

    assert isinstance(second, AlreadyRunning)
    assert second.run_id == first.id
    assert executor.started == [first.id]


def test_a_conversation_takes_the_projects_backend(db, task, executor):
    task.project.agent_backend = "something-else"
    db.commit()

    run = start_conversation(db, task.project)

    assert isinstance(run, Run)
    assert run.backend == "something-else"


def test_a_conversation_and_a_task_run_share_the_same_cap(db, task, executor, monkeypatch):
    """A standing conversation bills the same subscription window a task run
    does, so it gets no exemption from what protects that window."""
    monkeypatch.setenv("WORKBENCH_MAX_CONCURRENT_RUNS", "1")
    conversation = start_conversation(db, task.project)
    assert isinstance(conversation, Run)

    result = start_run(db, task, RunPhase.PLAN)

    assert isinstance(result, TooManyRuns)


# --- The concurrency cap ---------------------------------------------------


def test_the_cap_refuses_the_third_run(db, task, executor, monkeypatch):
    """Three taps on a phone should not start three agents."""
    monkeypatch.setenv("WORKBENCH_MAX_CONCURRENT_RUNS", "2")
    other = [_a_task(db, task, f"other {n}") for n in range(3)]

    first = start_run(db, other[0], RunPhase.PLAN)
    second = start_run(db, other[1], RunPhase.PLAN)
    third = start_run(db, other[2], RunPhase.PLAN)

    assert isinstance(first, Run)
    assert isinstance(second, Run)
    assert isinstance(third, TooManyRuns)
    assert "limit is 2" in third.message


def test_the_cap_can_be_lifted(db, task, executor, monkeypatch):
    monkeypatch.setenv("WORKBENCH_MAX_CONCURRENT_RUNS", "0")
    others = [_a_task(db, task, f"other {n}") for n in range(3)]

    results = [start_run(db, t, RunPhase.PLAN) for t in others]

    assert all(isinstance(r, Run) for r in results)


def test_a_finished_run_frees_its_slot(db, task, executor, monkeypatch):
    monkeypatch.setenv("WORKBENCH_MAX_CONCURRENT_RUNS", "1")
    first_task = _a_task(db, task, "first")
    second_task = _a_task(db, task, "second")
    first = start_run(db, first_task, RunPhase.PLAN)
    assert isinstance(first, Run)

    finish_run(db, first, RunStatus.SUCCEEDED)

    assert isinstance(start_run(db, second_task, RunPhase.PLAN), Run)


def test_one_task_cannot_be_worked_twice_at_once(db, task, executor):
    """One task, one worktree — two agents in it would overwrite each other."""
    first = start_run(db, task, RunPhase.PLAN)
    assert isinstance(first, Run)

    second = start_run(db, task, RunPhase.EXECUTE)

    assert isinstance(second, AlreadyRunning)
    assert second.run_id == first.id


# --- When the executor refuses ---------------------------------------------


def test_a_refused_start_leaves_a_failed_run_saying_why(db, task, monkeypatch):
    """The only trace of a polkit rule that never got installed."""
    monkeypatch.setattr(
        lifecycle,
        "get_executor",
        lambda _name=None: FakeExecutor(refuses="Could not start unit: access denied"),
    )

    result = start_run(db, task, RunPhase.PLAN)

    assert isinstance(result, NotStarted)
    run = db.get(Run, result.run_id)
    assert run is not None
    assert run.status is RunStatus.FAILED
    assert "access denied" in (run.error or "")


def test_an_unknown_executor_fails_the_run_rather_than_the_request(db, task, monkeypatch):
    monkeypatch.setattr(
        lifecycle, "get_executor", lambda _name=None: UnknownExecutor("gpu-node", ("systemd-unit",))
    )

    result = start_run(db, task, RunPhase.PLAN)

    assert isinstance(result, NotStarted)
    assert "gpu-node" in result.message


def test_a_refused_start_does_not_hold_a_slot(db, task, monkeypatch):
    monkeypatch.setenv("WORKBENCH_MAX_CONCURRENT_RUNS", "1")
    monkeypatch.setattr(lifecycle, "get_executor", lambda _name=None: FakeExecutor(refuses="nope"))
    first = _a_task(db, task, "first")
    second = _a_task(db, task, "second")

    start_run(db, first, RunPhase.PLAN)

    assert not isinstance(start_run(db, second, RunPhase.PLAN), TooManyRuns)


# --- Cancelling ------------------------------------------------------------


def test_cancelling_asks_the_executor_to_stop(db, task, executor):
    run = start_run(db, task, RunPhase.PLAN)
    assert isinstance(run, Run)

    cancel_run(db, run)

    assert executor.cancelled == ["fake-1"]


def test_cancelling_does_not_write_the_outcome_itself(db, task, executor):
    """The runner records `cancelled` when the signal arrives, having seen more."""
    run = start_run(db, task, RunPhase.PLAN)
    assert isinstance(run, Run)

    cancel_run(db, run)

    assert run.status is not RunStatus.CANCELLED


def test_a_run_that_never_started_is_cancelled_directly(db, task):
    """Nothing to be polite to, so the row is written here."""
    run = create_run(db, task, RunPhase.PLAN, backend="fake")

    result = cancel_run(db, run)

    assert isinstance(result, Run)
    assert result.status is RunStatus.CANCELLED


def test_cancelling_something_already_gone_records_it(db, task, monkeypatch):
    monkeypatch.setattr(lifecycle, "get_executor", lambda _name=None: FakeExecutor(alive=False))
    run = create_run(db, task, RunPhase.PLAN, backend="fake")
    record_launch(db, run, "fake", "fake-1")

    result = cancel_run(db, run)

    assert isinstance(result, Run)
    assert result.status is RunStatus.CANCELLED


def test_a_finished_run_cannot_be_cancelled(db, task):
    run = create_run(db, task, RunPhase.PLAN, backend="fake")
    finish_run(db, run, RunStatus.SUCCEEDED)

    assert isinstance(cancel_run(db, run), NotCancellable)


# --- Reaping ---------------------------------------------------------------


def test_a_run_whose_job_is_gone_is_failed(db, task, monkeypatch):
    monkeypatch.setattr(lifecycle, "get_executor", lambda _name=None: FakeExecutor(alive=False))
    run = create_run(db, task, RunPhase.PLAN, backend="fake")
    record_launch(db, run, "fake", "fake-1")
    age(db, run, 120)

    reaped = reap(db)

    assert [r.id for r in reaped] == [run.id]
    assert run.status is RunStatus.FAILED
    assert "without recording an outcome" in (run.error or "")


def test_a_live_run_is_left_alone(db, task, executor):
    run = start_run(db, task, RunPhase.PLAN)
    assert isinstance(run, Run)
    age(db, run, 120)

    assert reap(db) == []
    assert run.status is not RunStatus.FAILED


def test_a_run_younger_than_the_grace_period_is_not_judged(db, task, monkeypatch):
    """`start --no-block` returns before the unit exists; reaping then would kill it."""
    monkeypatch.setattr(lifecycle, "get_executor", lambda _name=None: FakeExecutor(alive=False))
    run = create_run(db, task, RunPhase.PLAN, backend="fake")
    record_launch(db, run, "fake", "fake-1")

    assert reap(db) == []
    assert run.status is RunStatus.QUEUED


def test_reaping_frees_the_slot_it_was_holding(db, task, monkeypatch):
    """Otherwise a killed run locks the machine out of starting any more."""
    monkeypatch.setenv("WORKBENCH_MAX_CONCURRENT_RUNS", "1")
    dead = create_run(db, task, RunPhase.PLAN, backend="fake")
    record_launch(db, dead, "fake", "fake-1")
    age(db, dead, 120)
    monkeypatch.setattr(lifecycle, "get_executor", lambda _name=None: FakeExecutor(alive=False))

    other = _a_task(db, task, "another")
    result = start_run(db, other, RunPhase.PLAN)

    assert not isinstance(result, TooManyRuns)
    assert dead.status is RunStatus.FAILED


def test_a_run_with_no_handle_is_not_reaped(db, task, monkeypatch):
    """It never started, so there is nothing to have died."""
    monkeypatch.setattr(lifecycle, "get_executor", lambda _name=None: FakeExecutor(alive=False))
    run = create_run(db, task, RunPhase.PLAN, backend="fake")
    age(db, run, 120)

    assert reap(db) == []


def _a_task(db, sibling, title: str):
    from workbench.database.models import Task

    task = Task(project_id=sibling.project_id, title=title)
    db.add(task)
    db.commit()
    return task
