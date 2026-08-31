"""Every write against a run and its event log, in one place.

The runner writes these while a run is in flight; the API and the event stream
read them. Keeping the writes here rather than inline in the runner is what
lets PR-3's cancel and reap paths move a run to a terminal state the same way
the runner does, instead of inventing a second spelling of "finished".

The transactions are deliberately small. A run lasts minutes, and holding one
SQLite write transaction open for its duration would block every other writer
on the machine for that long. More importantly the event log has to be
*readable while the run is still going* — that is the entire point of storing
it — and an uncommitted event is invisible to the reader tailing the stream.
So each event is its own committed transaction.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from workbench.database.models import (
    Project,
    Run,
    RunEvent,
    RunEventKind,
    RunInput,
    RunOutcome,
    RunPhase,
    RunStatus,
    Task,
)

logger = logging.getLogger(__name__)


def create_run(db: Session, task: Task, phase: RunPhase, backend: str) -> Run:
    """Record the intent to run, before anything is spawned.

    The row exists in `queued` so that the thing which starts the process has
    an id to hand it, and so a run that never starts is still visible rather
    than lost.
    """
    run = Run(task_id=task.id, phase=phase, backend=backend, status=RunStatus.QUEUED)
    db.add(run)
    db.commit()
    return run


def create_conversation(db: Session, project: Project, backend: str) -> Run:
    """Record the intent to talk to a project directly — the conversation
    counterpart to `create_run`, with a project instead of a task and no
    phase to choose (`RunPhase.CONVERSATION` is the only one there is)."""
    run = Run(
        project_id=project.id, phase=RunPhase.CONVERSATION, backend=backend, status=RunStatus.QUEUED
    )
    db.add(run)
    db.commit()
    return run


def create_task_conversation(db: Session, task: Task, backend: str) -> Run:
    """Talk to the agent that just worked a task, in the worktree it worked in.

    A conversation like `create_conversation`, but scoped to a task rather
    than a project, because that is what makes resuming possible at all: a
    backend's session token is keyed to the directory it ran in, so
    continuing a plan means running in that plan's worktree.

    Deliberately leaves `project_id` unset even though the project is
    reachable through the task. `active_run_for_project` selects on that
    column to find "the project's standing conversation", and this is not
    one — it belongs to a single task, and offering it as the project's
    would send someone talking about one task into a worktree for another.
    """
    run = Run(
        task_id=task.id, phase=RunPhase.CONVERSATION, backend=backend, status=RunStatus.QUEUED
    )
    db.add(run)
    db.commit()
    return run


def next_seq(db: Session, run_id: int) -> int:
    """The next sequence number for a run's log, starting at 1.

    Computed rather than counted from the caller's own bookkeeping, so a runner
    that resumes or retries cannot restart the numbering and collide. Only one
    process ever writes a given run's events, which is what makes read-then-
    insert safe here; the unique constraint on `(run_id, seq)` is the backstop
    if that assumption ever stops holding.
    """
    highest = db.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id))
    return (highest or 0) + 1


def append_event(
    db: Session, run_id: int, kind: RunEventKind, payload: dict | None = None
) -> RunEvent:
    """Add one event to a run's log and commit it immediately.

    Committed rather than batched because this table is how a run survives a
    restart and how the stream replays. An event still sitting in a transaction
    when the process dies never happened.
    """
    event = RunEvent(
        run_id=run_id,
        seq=next_seq(db, run_id),
        kind=kind,
        payload=payload or {},
    )
    db.add(event)
    db.commit()
    return event


def next_input_seq(db: Session, run_id: int) -> int:
    """The next sequence number for a run's typed-input queue.

    A separate counter from `next_seq`: `run_inputs` and `run_events` are
    different tables with different readers, and numbering them together
    would make neither sequence dense — the runner's poll and the browser's
    replay each need their own gapless count.
    """
    highest = db.scalar(select(func.max(RunInput.seq)).where(RunInput.run_id == run_id))
    return (highest or 0) + 1


def append_input(db: Session, run_id: int, body: str) -> RunInput:
    """Queue a message for the runner to deliver, committed immediately.

    Committed for the same reason `append_event` is: the runner polls this
    table from an entirely different process, and an uncommitted row is
    invisible to it.
    """
    row = RunInput(run_id=run_id, seq=next_input_seq(db, run_id), body=body)
    db.add(row)
    db.commit()
    return row


def fetch_new_inputs(db: Session, run_id: int, after_seq: int) -> list[RunInput]:
    """Everything typed into this run after `after_seq`, in order.

    The runner's poll loop calls this with the last sequence number it has
    already delivered, exactly how the browser's SSE replay resumes from
    `Last-Event-ID` against `run_events`.
    """
    return list(
        db.scalars(
            select(RunInput)
            .where(RunInput.run_id == run_id, RunInput.seq > after_seq)
            .order_by(RunInput.seq)
        ).all()
    )


def record_launch(db: Session, run: Run, executor: str, handle: str) -> Run:
    """Record how a run was started, and what to ask about it later.

    Written by whoever starts the run rather than by the runner itself, and
    before the process exists. The ordering matters: a run that is executing
    while nothing knows how to stop it is unreachable, so the handle is stored
    first and the process started second. The worst case is then a handle
    pointing at something that never started, which reaping notices.
    """
    run.executor = executor
    run.handle = handle
    db.commit()
    return run


def mark_running(db: Session, run: Run) -> Run:
    """The runner announcing that it has actually begun.

    Separate from `record_launch` because they are different facts told by
    different processes: one says how it was started, this says it is under
    way. Between them a run is `queued` with a handle, which is exactly what a
    unit that has been asked to start but has not reached ExecStart is.
    """
    run.status = RunStatus.RUNNING
    db.commit()
    append_event(db, run.id, RunEventKind.STATUS, {"status": RunStatus.RUNNING.value})
    return run


def report_outcome(db: Session, run: Run, outcome: RunOutcome, detail: str | None = None) -> Run:
    """Record what the agent itself says happened, while the run is still going.

    Written live, through the outcome API, rather than only at the end: the
    whole point is that this decision survives a crash or a deploy restart
    that kills the process before it would otherwise report anything, the
    same reasoning that makes every other run event its own committed
    transaction rather than something batched.
    """
    run.agent_outcome = outcome
    if detail is not None:
        run.outcome_detail = detail
    db.commit()
    return run


def finish_run(
    db: Session,
    run: Run,
    status: RunStatus,
    *,
    plan: str | None = None,
    summary: str | None = None,
    diffstat: str | None = None,
    error: str | None = None,
    resume_token: str | None = None,
    model: str | None = None,
    total_cost_usd: float | None = None,
    num_turns: int | None = None,
    outcome_detail: str | None = None,
    proposed_subtasks: dict | None = None,
    pr_url: str | None = None,
) -> Run:
    """Record how a run ended, whatever the ending was.

    Every field is optional and only written when given, so a failure partway
    through still records the resume token and usage it did learn — those are
    the two things that genuinely cannot be reconstructed afterwards.

    `finished_at` is set for `awaiting_review` too. The run really has stopped;
    the *task* is what is unfinished, and `RunStatus.is_terminal` already draws
    that distinction for anyone who needs it.
    """
    run.status = status
    if plan is not None:
        run.plan = plan
    if summary is not None:
        run.summary = summary
    if diffstat is not None:
        run.diffstat = diffstat
    if error is not None:
        run.error = error
    if resume_token is not None:
        run.resume_token = resume_token
    if model is not None:
        run.model = model
    if total_cost_usd is not None:
        run.total_cost_usd = total_cost_usd
    if num_turns is not None:
        run.num_turns = num_turns
    if outcome_detail is not None:
        run.outcome_detail = outcome_detail
    if proposed_subtasks is not None:
        run.proposed_subtasks = proposed_subtasks
    if pr_url is not None:
        run.pr_url = pr_url
    run.finished_at = datetime.now(UTC)
    # The job is over either way, and a stale handle is worse than none: a pid
    # will eventually belong to something else entirely, and a unit name will
    # be reused by nothing but still invites a pointless stop.
    run.handle = None
    db.commit()
    append_event(db, run.id, RunEventKind.STATUS, {"status": status.value})
    logger.info("Run %s finished: %s", run.id, status.value)
    return run
