"""Starting, cancelling, and reaping runs.

The web tier never executes an agent. It writes a `runs` row, spawns a detached
process, and from then on only reads. Everything in this module is about that
boundary: what has to be true before a run may start, how to stop one, and how
to notice one that died without saying so.
"""

import logging
import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.config import max_concurrent_runs
from workbench.models import Project, Run, Task
from workbench.worktrees import GitFailed, WorktreeReady, ensure_worktree, run_setup_command

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunStarted:
    run: Run


@dataclass(frozen=True)
class RunRefused:
    """The run did not start, for a reason worth showing the user."""

    message: str


type StartResult = RunStarted | RunRefused


def active_run_count(db: Session) -> int:
    return len(db.scalars(select(Run.id).where(Run.status.in_(("queued", "running")))).all())


def runnable_check(db: Session, task: Task) -> str | None:
    """Why this task cannot be run right now, or None if it can be.

    Checked before any work happens so the reason lands on screen immediately
    rather than after an agent has spent minutes on it.
    """
    if task.children:
        return (
            "This task has sub-tasks, so there is no single piece of work to hand to "
            "an agent. Run one of its sub-tasks instead."
        )
    if task.status in ("done", "cancelled"):
        return f"This task is {task.status}. Reopen it before starting a run."
    if task.project.local_path is None:
        return "This project has not been cloned to the server yet."
    if any(run.status in ("queued", "running") for run in task.runs):
        return "A run is already in progress for this task."
    if active_run_count(db) >= max_concurrent_runs():
        return (
            f"{max_concurrent_runs()} runs are already in flight, which is the "
            "configured limit. Wait for one to finish."
        )
    return None


def prepare_worktree(task: Task, project: Project) -> str | None:
    """Give the task a branch and a worktree. Returns an error message or None.

    Idempotent: a task that already has a worktree keeps it, which is what lets
    the execute phase resume the planning session in the directory it ran in.
    """
    if task.worktree_path and Path(task.worktree_path).exists():
        return None

    assert project.local_path is not None  # guarded by runnable_check
    result = ensure_worktree(
        repo=Path(project.local_path),
        task_id=task.id,
        title=task.title,
        base_branch=project.default_branch or "main",
    )
    if isinstance(result, GitFailed):
        return f"{result.message} {result.stderr}".strip()

    assert isinstance(result, WorktreeReady)
    task.branch = result.branch
    task.worktree_path = str(result.path)

    if project.setup_command:
        setup = run_setup_command(result.path, project.setup_command)
        if isinstance(setup, GitFailed):
            # Not fatal. The agent may not need whatever the setup command
            # provides, and failing the run here would hide the real task
            # behind an environment problem.
            logger.warning("Setup command failed in %s: %s", result.path, setup.stderr)

    return None


def _runner_command(run_id: int) -> list[str]:
    """The command that executes a run.

    `sys.executable` rather than `uv run`: the service runs from the venv
    directly and must not depend on uv being installed or on PATH at boot.
    """
    return [sys.executable, "-m", "workbench.runner", str(run_id)]


def start_run(db: Session, task: Task, phase: str, session_id: str | None = None) -> StartResult:
    """Create a run row and spawn the detached process that executes it."""
    refusal = runnable_check(db, task)
    if refusal is not None:
        return RunRefused(refusal)

    error = prepare_worktree(task, task.project)
    if error is not None:
        return RunRefused(error)

    run = Run(task_id=task.id, phase=phase, status="queued", session_id=session_id)
    db.add(run)
    task.status = "active"
    db.commit()

    try:
        process = subprocess.Popen(
            _runner_command(run.id),
            cwd=str(Path(__file__).resolve().parents[2]),
            # start_new_session detaches the child into its own process group,
            # so it survives the web process exiting and is not killed by a
            # signal sent to uvicorn.
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        message = f"Could not start the runner process: {error}"
        run.status = "failed"
        run.error = message
        run.finished_at = datetime.now(UTC)
        db.commit()
        return RunRefused(message)

    run.pid = process.pid
    db.commit()
    return RunStarted(run)


def cancel_run(db: Session, run: Run) -> str | None:
    """Stop a running run. Returns an error message, or None on success.

    SIGTERM to the process group rather than the process: the agent spawns
    build tools and test runners of its own, and signalling only the runner
    would leave those behind.
    """
    if run.status not in ("queued", "running"):
        return f"This run is already {run.status}."

    if run.pid is not None:
        # Already gone, or not ours: either way there is nothing left to stop
        # and the row below is the thing that needs correcting.
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(run.pid), signal.SIGTERM)

    run.status = "cancelled"
    run.finished_at = datetime.now(UTC)
    db.commit()
    return None


def reap_stale_runs(db: Session) -> None:
    """Mark runs whose process has vanished as failed.

    A runner killed by OOM or `kill -9` never gets to record an outcome, and
    without this its row stays `running` forever — the task looks permanently
    busy and cannot be restarted.
    """
    stale = db.scalars(select(Run).where(Run.status.in_(("queued", "running")))).all()
    changed = False

    for run in stale:
        if run.pid is None:
            continue
        try:
            # Signal 0 checks for existence without delivering anything.
            os.kill(run.pid, 0)
        except ProcessLookupError:
            run.status = "failed"
            run.error = "The runner process exited without recording a result."
            run.finished_at = datetime.now(UTC)
            changed = True
        except PermissionError:
            # The pid exists but belongs to another user, which means it has
            # been recycled since. Treat that as gone.
            run.status = "failed"
            run.error = "The runner process is no longer running."
            run.finished_at = datetime.now(UTC)
            changed = True

    if changed:
        db.commit()
