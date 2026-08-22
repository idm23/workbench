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
    Run,
    RunEvent,
    RunEventKind,
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


def start_run(db: Session, run: Run, pid: int) -> Run:
    """Mark a queued run as running, recording the process that owns it.

    The pid is what makes cancelling possible and what lets a later reap tell
    "still working" from "died without saying so".
    """
    run.status = RunStatus.RUNNING
    run.pid = pid
    db.commit()
    append_event(db, run.id, RunEventKind.STATUS, {"status": RunStatus.RUNNING.value})
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
    run.finished_at = datetime.now(UTC)
    # The process is over either way, and a stale pid is worse than none: it
    # will eventually belong to something else entirely.
    run.pid = None
    db.commit()
    append_event(db, run.id, RunEventKind.STATUS, {"status": status.value})
    logger.info("Run %s finished: %s", run.id, status.value)
    return run
