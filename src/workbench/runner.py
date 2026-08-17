"""The detached process that executes one run.

Invoked as `python -m workbench.runner <run_id>` and deliberately not run
inside uvicorn. Agent runs take minutes; the web process gets restarted by
deploys, crashes, and OOM kills. Keeping them separate means a restart cannot
orphan a run, and it is what makes Workbench safe to deploy from inside
Workbench — the request that triggers a deploy is not the process doing the
work.

Every message is committed to `run_events` as it arrives rather than buffered,
so the web tier can render a run's progress by reading the database and nothing
is lost if this process dies.
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import anyio
from anyio.to_thread import run_sync
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from workbench.agent import (
    MAX_TURNS_EXECUTE,
    MAX_TURNS_PLAN,
    AgentEvent,
    AgentFinished,
    AgentUnavailable,
    execute_prompt,
    plan_prompt,
    run_agent,
)
from workbench.db import get_session_factory
from workbench.github import (
    PullRequestOpened,
    Pushed,
    RepoRef,
    open_pull_request,
    push_branch,
    redact,
)
from workbench.logs import configure_console_logging
from workbench.models import Run, RunEvent
from workbench.worktrees import diffstat, has_commits, uncommitted_diffstat

logger = logging.getLogger(__name__)


def _append_event(db: Session, run_id: int, kind: str, payload: dict) -> None:
    """Persist one event and commit immediately.

    Committing per event is the point: a buffered write would be lost exactly
    when it matters most, which is when this process dies unexpectedly.
    """
    next_seq = db.scalar(
        select(func.coalesce(func.max(RunEvent.seq), 0) + 1).where(RunEvent.run_id == run_id)
    )
    db.add(
        RunEvent(
            run_id=run_id,
            seq=next_seq or 1,
            kind=kind,
            payload=json.dumps(payload, default=str),
        )
    )
    db.commit()


def _finish(db: Session, run: Run, status: str, error: str | None = None) -> None:
    run.status = status
    run.error = redact(error) if error else None
    run.finished_at = datetime.now(UTC)
    db.commit()
    _append_event(db, run.id, "status", {"status": status, "error": run.error})


async def _drive(db: Session, run: Run, prompt: str, mode: str, turns: int, resume: str | None):
    """Run the agent to completion, storing everything it emits."""
    outcome: AgentFinished | AgentUnavailable | None = None
    worktree = Path(run.task.worktree_path or ".")

    async for item in run_agent(
        prompt=prompt,
        cwd=worktree,
        permission_mode=mode,
        max_turns=turns,
        resume=resume,
    ):
        match item:
            case AgentEvent():
                _append_event(db, run.id, item.kind, item.payload)
            case AgentFinished() | AgentUnavailable():
                outcome = item

    return outcome


async def _run_plan_phase(db: Session, run: Run) -> None:
    task = run.task
    outcome = await _drive(
        db,
        run,
        prompt=plan_prompt(task.title, task.body),
        # Plan mode is enforced by the CLI, not by the prompt: the agent cannot
        # edit files in this phase even if it decides it should.
        mode="plan",
        turns=MAX_TURNS_PLAN,
        resume=None,
    )

    if outcome is None:
        _finish(db, run, "failed", "The agent stopped without reporting a result.")
        return
    if isinstance(outcome, AgentUnavailable):
        _finish(db, run, "failed", outcome.message)
        return

    run.plan = outcome.text
    run.session_id = outcome.session_id
    run.total_cost_usd = outcome.total_cost_usd
    run.num_turns = outcome.num_turns

    if outcome.is_error:
        _finish(db, run, "failed", outcome.text or "The planning run reported an error.")
        return

    # The one run status that is not terminal: it is waiting for a person.
    _finish(db, run, "awaiting_review")


async def _run_execute_phase(db: Session, run: Run) -> None:
    task = run.task
    project = task.project

    outcome = await _drive(
        db,
        run,
        prompt=execute_prompt(task.title),
        # bypassPermissions, and this is not a shortcut. `acceptEdits` permits
        # file edits but still gates Bash, so the agent cannot run `git commit`
        # — it burned 33 turns retrying before failing, which is how this was
        # found. There is no one at a terminal to approve anything, and a run
        # that cannot commit cannot produce a pull request, which is the whole
        # point of this phase.
        #
        # The bound on what an agent can do is therefore the account it runs
        # as, not the permission mode. That is the argument for the dedicated
        # unprivileged `workbench` user, which is still deferred.
        mode="bypassPermissions",
        turns=MAX_TURNS_EXECUTE,
        resume=run.session_id,
    )

    if outcome is None:
        _finish(db, run, "failed", "The agent stopped without reporting a result.")
        return
    if isinstance(outcome, AgentUnavailable):
        _finish(db, run, "failed", outcome.message)
        return

    run.summary = outcome.text
    run.total_cost_usd = outcome.total_cost_usd
    run.num_turns = outcome.num_turns
    db.commit()

    worktree = Path(task.worktree_path or "")
    base = project.default_branch or "main"

    # Committed work first; uncommitted changes are reported too, so an
    # interrupted run still shows what it touched.
    stat = diffstat(worktree, base)
    pending = uncommitted_diffstat(worktree)
    if pending:
        stat = f"{stat}\n\nUncommitted:\n{pending}".strip()
    run.diffstat = stat
    db.commit()

    if outcome.is_error:
        _finish(db, run, "failed", outcome.text or "The run reported an error.")
        return

    if not has_commits(worktree, base):
        _finish(
            db,
            run,
            "failed",
            "The agent finished without committing anything, so there is nothing to "
            "open a pull request from. Its summary is recorded above.",
        )
        return

    await _publish(db, run, project.owner, project.repo, task.branch or "", base, task.title)


async def _publish(
    db: Session,
    run: Run,
    owner: str,
    repo: str,
    branch: str,
    base: str,
    title: str,
) -> None:
    """Push the branch and open the pull request.

    Failures here are recorded but do not discard the run: the commits exist in
    the worktree either way, and the summary is already stored.
    """
    ref = RepoRef(owner=owner, repo=repo)
    worktree = Path(run.task.worktree_path or "")

    _append_event(db, run.id, "system", {"subtype": "pushing", "branch": branch})
    # Pushing and the pull request API call are blocking, and this coroutine is
    # driving the agent's event stream — running them inline would stall it.
    pushed = await run_sync(push_branch, worktree, ref, branch)
    if not isinstance(pushed, Pushed):
        _finish(
            db,
            run,
            "failed",
            f"Work is committed locally but the push failed. {pushed.message}",
        )
        return

    body = _pull_request_body(run)
    opened = await run_sync(open_pull_request, ref, branch, base, title, body)
    if isinstance(opened, PullRequestOpened):
        run.pr_url = opened.url
        db.commit()
        _append_event(db, run.id, "system", {"subtype": "pr_opened", "url": opened.url})
        run.task.status = "done"
        db.commit()
        _finish(db, run, "succeeded")
        return

    _finish(
        db,
        run,
        "failed",
        f"The branch is pushed but no pull request was opened. {opened.message}",
    )


def _pull_request_body(run: Run) -> str:
    """The pull request description: the agent's own summary, plus provenance."""
    parts = [run.summary or "_No summary was produced._"]
    if run.diffstat:
        parts += ["", "## Changes", "", "```", run.diffstat.strip(), "```"]
    parts += [
        "",
        "---",
        f"Generated by Workbench run #{run.id} for task #{run.task_id}.",
    ]
    return "\n".join(parts)


async def _main(run_id: int) -> int:
    session_factory = get_session_factory()
    db = session_factory()
    try:
        run = db.get(Run, run_id)
        if run is None:
            logger.error("No run with id %s.", run_id)
            return 1

        if run.status not in ("queued",):
            logger.error("Run %s is %s, not queued; refusing to start.", run_id, run.status)
            return 1

        run.status = "running"
        run.pid = os.getpid()
        db.commit()
        _append_event(db, run.id, "status", {"status": "running", "phase": run.phase})

        if run.phase == "plan":
            await _run_plan_phase(db, run)
        else:
            await _run_execute_phase(db, run)

        logger.info("Run %s finished: %s", run_id, run.status)
        return 0 if run.status in ("succeeded", "awaiting_review") else 1
    finally:
        db.close()


def main() -> int:
    configure_console_logging()
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        logger.error("Usage: python -m workbench.runner <run_id>")
        return 2
    return anyio.run(_main, int(sys.argv[1]))


if __name__ == "__main__":
    raise SystemExit(main())
