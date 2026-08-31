"""Starting, stopping, and reaping runs.

The web tier calls these and then goes back to reading. Everything here is
about the *row* — deciding whether a run may start, recording how it was
started, and noticing when one has stopped without saying so. What happens
during a run belongs to `runner.py`, in another process entirely.

Results rather than exceptions throughout, as elsewhere: "you already have two
agents going" and "this task is already being worked" are ordinary answers to a
button press, not failures.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.config import default_agent_backend, max_concurrent_runs
from workbench.database.models import Project, Run, RunEventKind, RunPhase, RunStatus, Task
from workbench.runs.executors import Started, UnknownExecutor, get_executor
from workbench.runs.store import (
    append_event,
    create_conversation,
    create_run,
    create_task_conversation,
    finish_run,
    record_launch,
)

logger = logging.getLogger(__name__)

#: How long a run is left alone before reaping will believe its executor.
#:
#: There is a window between recording a handle and the job actually existing —
#: systemd has accepted `start --no-block` but has not reached ExecStart — and
#: during it the executor honestly answers "not running". Reaping inside that
#: window would kill every run at the moment it was started.
REAP_GRACE_SECONDS = 30


@dataclass(frozen=True)
class TooManyRuns:
    """The concurrency cap refused this one."""

    active: int
    limit: int

    @property
    def message(self) -> str:
        return (
            f"{self.active} runs are already going and the limit is {self.limit}. "
            "Wait for one to finish, or cancel it."
        )


@dataclass(frozen=True)
class AlreadyRunning:
    """This task already has a run in flight.

    Refused because a task has one worktree, and two agents in one checkout
    would overwrite each other's work while both believed they were alone.
    """

    run_id: int

    @property
    def message(self) -> str:
        return f"Run {self.run_id} is already working this task."


@dataclass(frozen=True)
class NotStarted:
    """The executor would not start it, and the run is recorded as failed.

    The row is kept rather than deleted: "we tried and the unit would not
    start" is worth seeing, and it is the only trace of a polkit rule that
    never got installed.
    """

    run_id: int
    message: str


type StartResult = Run | TooManyRuns | AlreadyRunning | NotStarted


def active_runs(db: Session) -> list[Run]:
    """Every run holding a concurrency slot, across all projects."""
    return list(
        db.scalars(
            select(Run)
            .where(Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
            .order_by(Run.id)
        ).all()
    )


def active_run_for_task(db: Session, task_id: int) -> Run | None:
    return db.scalars(
        select(Run)
        .where(Run.task_id == task_id, Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
        .order_by(Run.id.desc())
        .limit(1)
    ).first()


def active_run_for_project(db: Session, project_id: int) -> Run | None:
    """The project's own in-flight conversation, if it has one.

    Mirrors `active_run_for_task` exactly — a project's conversation is a
    run like any other, just scoped to `project_id` instead of `task_id`.
    """
    return db.scalars(
        select(Run)
        .where(Run.project_id == project_id, Run.status.in_([RunStatus.QUEUED, RunStatus.RUNNING]))
        .order_by(Run.id.desc())
        .limit(1)
    ).first()


def _launch(db: Session, run: Run, executor: str | None) -> StartResult:
    """Hand an already-created run to an executor, or explain why not.

    The part of starting a run that is identical whichever kind it is —
    task-scoped or a project's own conversation — once the row itself
    exists and has passed the concurrency check.
    """
    implementation = get_executor(executor)
    if isinstance(implementation, UnknownExecutor):
        finish_run(db, run, RunStatus.FAILED, error=implementation.message)
        return NotStarted(run.id, implementation.message)

    # Recorded before the job exists, so there is never a moment where
    # something is running and nothing knows how to stop it.
    record_launch(db, run, implementation.name, _handle_for(implementation, run.id))
    append_event(
        db,
        run.id,
        RunEventKind.STATUS,
        {"status": RunStatus.QUEUED.value, "executor": implementation.name},
    )

    outcome = implementation.start(run.id)
    if not isinstance(outcome, Started):
        finish_run(db, run, RunStatus.FAILED, error=outcome.message)
        return NotStarted(run.id, outcome.message)

    # The executor is the authority on its own handle; the pre-recorded one is
    # a prediction so that a crash between here and there is still reachable.
    if outcome.handle != run.handle:
        record_launch(db, run, implementation.name, outcome.handle)
    return run


def start_run(
    db: Session,
    task: Task,
    phase: RunPhase,
    *,
    backend: str | None = None,
    executor: str | None = None,
) -> StartResult:
    """Begin a run, or explain why not.

    Reaping first is deliberate. A run whose process died without recording an
    outcome still looks active, and would hold a slot against the cap forever —
    so the cheapest moment to notice is when someone is asking for a slot.
    """
    reap(db)

    existing = active_run_for_task(db, task.id)
    if existing is not None:
        return AlreadyRunning(existing.id)

    limit = max_concurrent_runs()
    running = active_runs(db)
    if limit > 0 and len(running) >= limit:
        return TooManyRuns(len(running), limit)

    chosen = backend or task.project.agent_backend or default_agent_backend()
    run = create_run(db, task, phase, backend=chosen)
    return _launch(db, run, executor)


@dataclass(frozen=True)
class NotContinuable:
    """This run cannot be reopened, and the reason is worth showing."""

    message: str


def continue_run(
    db: Session,
    source: Run,
    *,
    executor: str | None = None,
) -> StartResult | NotContinuable:
    """Reopen a finished run as a conversation, because someone asked to.

    The counterpart to plan and execute runs ending the moment the agent is
    done. Ending promptly is only reasonable if picking the thread back up is
    possible, and making that a button rather than a five-minute window is
    the point: a dialog is now something a person chooses, not something
    every run waits around for on the chance that one is wanted.

    A new run rather than a resurrection of this one. The old run is a record
    of what happened and stays that way — its events, its cost, its outcome —
    and continuing it would rewrite history that something else may already
    have reported on.
    """
    reap(db)

    task = source.task
    if task is None:
        return NotContinuable("Only a run that belongs to a task can be continued.")
    if not source.status.is_terminal:
        return NotContinuable("That run has not finished yet.")
    if source.resume_token is None:
        # Nothing to resume into. Starting cold would look identical from
        # the outside and answer from no context at all, which is worse than
        # saying so.
        return NotContinuable("That run left no session to continue.")

    existing = active_run_for_task(db, task.id)
    if existing is not None:
        return AlreadyRunning(existing.id)

    limit = max_concurrent_runs()
    running = active_runs(db)
    if limit > 0 and len(running) >= limit:
        return TooManyRuns(len(running), limit)

    # The source run's backend, never the project's current default: the token
    # is opaque and means nothing to any backend but the one that issued it.
    run = create_task_conversation(db, task, backend=source.backend)
    return _launch(db, run, executor)


def start_conversation(
    db: Session,
    project: Project,
    *,
    backend: str | None = None,
    executor: str | None = None,
) -> StartResult:
    """Begin (or report) the project's own conversation.

    Shares `active_runs`/`max_concurrent_runs` with task runs rather than a
    cap of its own — a conversation held open bills the same subscription
    window a task run does, so it is not free to leave open, and does not
    get an exemption from what protects that window.
    """
    reap(db)

    existing = active_run_for_project(db, project.id)
    if existing is not None:
        return AlreadyRunning(existing.id)

    limit = max_concurrent_runs()
    running = active_runs(db)
    if limit > 0 and len(running) >= limit:
        return TooManyRuns(len(running), limit)

    chosen = backend or project.agent_backend or default_agent_backend()
    run = create_conversation(db, project, backend=chosen)
    return _launch(db, run, executor)


def _handle_for(implementation, run_id: int) -> str:
    """The handle a run will have, where that is knowable in advance.

    A systemd unit's name is derived from the run id, so it can be written
    before the unit exists. A pid cannot be, so the local executor gets a
    placeholder that its own `start` immediately replaces.
    """
    from workbench.config import run_unit_name
    from workbench.runs.executors import SystemdUnitExecutor

    if isinstance(implementation, SystemdUnitExecutor):
        return run_unit_name(run_id)
    return ""


@dataclass(frozen=True)
class NotCancellable:
    """The run has already stopped."""

    message: str


def cancel_run(db: Session, run: Run) -> Run | NotCancellable:
    """Ask a run to stop.

    Asking rather than killing: a stop request reaches the runner as SIGTERM,
    which it catches to record `cancelled` and let the agent's own child wind
    up. The row is only written by this function when there was nothing to ask
    — a queued run that never started has no process to be polite to.
    """
    if run.status.is_terminal:
        return NotCancellable(f"Run {run.id} already {run.status.value}.")

    if not run.handle:
        return finish_run(db, run, RunStatus.CANCELLED, error="Cancelled before it started.")

    implementation = get_executor(run.executor)
    if isinstance(implementation, UnknownExecutor):
        # The executor that started it is gone from this machine, so nothing
        # can stop it politely. Recording the truth beats leaving it `running`.
        return finish_run(db, run, RunStatus.CANCELLED, error=implementation.message)

    append_event(db, run.id, RunEventKind.NOTICE, {"text": "Cancellation requested."})
    if not implementation.cancel(run.handle):
        # The job is already gone. Reaping would catch this eventually; saying
        # so now is the same answer sooner.
        return finish_run(
            db, run, RunStatus.CANCELLED, error="The run was no longer running when cancelled."
        )

    # Deliberately not marked here. The runner writes `cancelled` itself when
    # the signal arrives, which records what it managed to do first — and if it
    # never does, reaping notices.
    return run


def reap(db: Session, grace_seconds: int = REAP_GRACE_SECONDS) -> list[Run]:
    """Finish runs whose job is gone but whose row still says otherwise.

    This is what keeps the concurrency cap honest. A killed process, an OOM, a
    machine rebooted mid-run — none of them get to write an outcome, and
    without this the row says `running` forever with a handle pointing at
    nothing.

    Returns what it reaped, so a caller can log it.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=grace_seconds)
    reaped: list[Run] = []

    for run in active_runs(db):
        created = run.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created > cutoff:
            # Too young to judge: it may not have reached ExecStart yet.
            continue
        if not run.handle:
            continue

        implementation = get_executor(run.executor)
        if isinstance(implementation, UnknownExecutor) or implementation.is_running(run.handle):
            continue

        logger.warning("Reaping run %s: %s is gone.", run.id, run.handle)
        finish_run(
            db,
            run,
            RunStatus.FAILED,
            error=(
                f"The run stopped without recording an outcome ({run.handle} is no longer "
                "running). It was most likely killed."
            ),
        )
        reaped.append(run)

    return reaped
