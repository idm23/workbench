"""The detached process that actually runs a task.

Invoked as `python -m workbench.runs.runner <run_id>` against a run row that
already exists. Nothing here is reachable from the web process: the runner owns
persistence, the web tier is a pure reader, and the only thing passed between
them is a row id.

That split is not tidiness. Agent runs outliving the web process is a
prerequisite rather than a nicety, because deploys land on a timer with nobody
watching — see CLAUDE.md's self-deployment trap. Every interesting thing this
process learns is committed to `run_events` as it happens, so a run that is
killed halfway is still legible afterwards, and a phone that reconnects can
replay what it missed.

The one thing this module must never do is exit without recording an outcome.
A run stuck in `running` with a pid that no longer exists holds a concurrency
slot forever and tells whoever reads it nothing.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.agents.prompts import prompt_for
from workbench.agents.protocol import (
    AgentEvent,
    AgentFailed,
    AgentFinished,
    AgentOutcome,
    AgentRequest,
    AgentUnavailable,
    Backend,
    SubtaskProposal,
)
from workbench.agents.registry import UnknownBackend, get_backend
from workbench.config import agent_environment, billing_mode, input_idle_seconds
from workbench.database.db import session_scope
from workbench.database.models import (
    Run,
    RunEventKind,
    RunOutcome,
    RunPhase,
    RunStatus,
    Task,
    TaskStatus,
)
from workbench.git.worktrees import (
    GitFailed,
    diffstat,
    ensure_worktree,
    fetch_checkout,
    local_checkout,
    run_setup_command,
    uncommitted_diffstat,
)
from workbench.runs.store import append_event, fetch_new_inputs, finish_run, mark_running
from workbench.tasks.origin import InvalidOrigin, origin_branch_for, resolve_origin

#: How often to check for something typed into the run, matching the cadence
#: `stream.py` already polls `run_events` at for the browser's own tailing.
INPUT_POLL_SECONDS = 1.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Prepared:
    """Everything the agent needs, once the checkout is ready for it."""

    backend: Backend
    request: AgentRequest


@dataclass(frozen=True)
class NotPrepared:
    """The run could not be started, and nothing was attempted.

    Separate from a failed agent for the same reason `AgentUnavailable` is: no
    model ran, so there is no summary to write, no cost to record, and the
    worktree is exactly as it was.
    """

    message: str


@dataclass(frozen=True)
class Interrupted:
    """The process was signalled before the agent finished.

    A deploy restarting the service is the ordinary cause, which is why this is
    an expected ending rather than a crash. Whatever the agent committed is
    still on the branch.
    """

    signal_name: str


type Ending = AgentOutcome | Interrupted


def resume_token_for(db: Session, task: Task, backend: str) -> str | None:
    """The token that continues this task's earlier conversation, if any.

    Filtered by backend, never merely by recency: the token is opaque and means
    nothing to a backend other than the one that issued it, so handing a
    Claude token to some future backend would be worse than starting cold.

    Scoped to the task rather than the run because a plan run and the execute
    run that follows it are two attempts at one piece of work sharing one
    worktree — which is exactly why `branch` and `worktree_path` live on tasks.
    """
    return db.scalar(
        select(Run.resume_token)
        .where(
            Run.task_id == task.id,
            Run.backend == backend,
            Run.resume_token.is_not(None),
        )
        .order_by(Run.id.desc())
        .limit(1)
    )


def prepare(db: Session, run: Run) -> Prepared | NotPrepared:
    """Get the worktree and the backend ready, or explain why not.

    Each step reports into the event log before it runs. A first clone or a
    project's setup command can take minutes, and without this the stream would
    sit blank long enough to look broken.
    """
    task = run.task
    project = task.project

    checkout = local_checkout(project.owner, project.repo)
    if checkout is None:
        return NotPrepared(
            f"{project.owner}/{project.repo} has not been cloned onto this machine yet."
        )

    if task.worktree_path is None:
        # Only resolved and only fetched here: once a worktree exists its
        # branch is fixed, `ensure_worktree` below returns it without even
        # looking at `base_branch`, and re-fetching a large repository on
        # every subsequent execute run would cost time for no benefit.
        resolved = resolve_origin(task, task.origin_ref)
        if isinstance(resolved, InvalidOrigin):
            return NotPrepared(resolved.message)
        base_branch = resolved

        fetched = fetch_checkout(checkout)
        if isinstance(fetched, GitFailed):
            return NotPrepared(f"{fetched.message} {fetched.stderr}".strip())

        notice = {"text": f"Preparing worktree from {base_branch}."}
    else:
        # `ensure_worktree` returns the existing worktree without consulting
        # `base_branch` at all once its path is already there, so this is a
        # placeholder rather than a real choice — the branch was fixed the
        # first time this task was prepared.
        base_branch = task.branch or (project.default_branch or "main")
        notice = {"text": "Reusing the existing worktree."}

    append_event(db, run.id, RunEventKind.NOTICE, notice)

    worktree = ensure_worktree(checkout, task.id, task.title, base_branch)
    if isinstance(worktree, GitFailed):
        return NotPrepared(f"{worktree.message} {worktree.stderr}".strip())

    # Recorded on the task, not the run: the execute run reuses this checkout,
    # and deleting the task is what cleans it up.
    task.branch = worktree.branch
    task.worktree_path = str(worktree.path)
    db.commit()

    if project.setup_command:
        notice = {"text": "Running the project setup command."}
        append_event(db, run.id, RunEventKind.NOTICE, notice)
        setup = run_setup_command(worktree.path, project.setup_command)
        if isinstance(setup, GitFailed):
            return NotPrepared(f"{setup.message} {setup.stderr}".strip())

    backend = get_backend(run.backend)
    if isinstance(backend, UnknownBackend):
        return NotPrepared(backend.message)

    return Prepared(
        backend=backend,
        request=AgentRequest(
            worktree=worktree.path,
            phase=run.phase,
            prompt=prompt_for(run.phase, task.title, task.body),
            resume_token=resume_token_for(db, task, run.backend),
            model=run.model,
            run_id=run.id,
            task_id=task.id,
        ),
    )


@dataclass
class _Activity:
    """The last moment anything happened on this run — agent output or
    typed input — as a plain monotonic timestamp shared between the event
    loop and the input watcher below."""

    last: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last = time.monotonic()


async def _watch_for_input(
    db: Session, run_id: int, activity: _Activity, idle_seconds: float
) -> AsyncIterator[str]:
    """Everything typed into this run, as it arrives, until it goes quiet.

    Polls `run_inputs` the same way `stream.py` polls `run_events` for the
    browser — this process and whatever handles the `POST /runs/{id}/message`
    route are different processes, so there is nothing to await but the
    table. Stops (closing the generator) once neither this nor the agent's
    own output has touched `activity` for `idle_seconds`, which is what lets
    a conversation actually end rather than poll forever: `query()` in
    `agents/claude.py` only stops once this generator does.

    Fetches *before* checking the idle deadline, not after: checking first
    would let a row committed just as the deadline passes be missed even
    though it was there the whole time this loop was already going to look —
    a real race when `idle_seconds` and the poll cadence are close in
    magnitude, found by a genuinely concurrent smoke test rather than assumed
    safe from the unit tests alone.
    """
    last_seq = 0
    while True:
        for row in fetch_new_inputs(db, run_id, after_seq=last_seq):
            last_seq = row.seq
            activity.touch()
            yield row.body
        if time.monotonic() - activity.last > idle_seconds:
            return
        await asyncio.sleep(INPUT_POLL_SECONDS)


async def drive(db: Session, run_id: int, prepared: Prepared) -> Ending:
    """Run the agent, committing every event as it arrives.

    The database writes are synchronous inside the event loop deliberately.
    This process does one thing, the writes are local and sub-millisecond, and
    the alternative — a thread hop per event — would cost more than it saves
    while making the ordering harder to reason about.

    SIGTERM is caught rather than fatal because the ordinary cause is a deploy
    restarting the service, and a run that vanishes without a word is the thing
    the event log exists to prevent.
    """
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    caught: list[str] = []

    def on_signal(name: str) -> None:
        caught.append(name)
        if current is not None:
            current.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_signal, sig.name)

    # A long tool-call loop with nothing typed into it must not let the idle
    # clock expire mid-turn — every event touches it, not only new input.
    activity = _Activity()
    request = replace(
        prepared.request,
        inputs=_watch_for_input(db, run_id, activity, input_idle_seconds()),
    )

    outcome: AgentOutcome | None = None
    try:
        async for item in prepared.backend.run(request):
            if isinstance(item, AgentEvent):
                activity.touch()
                append_event(db, run_id, item.kind, item.payload)
            else:
                outcome = item
    except asyncio.CancelledError:
        return Interrupted(caught[0] if caught else "cancelled")
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)

    if outcome is None:
        # The protocol says a backend always yields one. A backend that does
        # not is broken, and saying so beats a run that silently succeeds.
        return AgentFailed("The backend produced no outcome.")
    return outcome


def _worktree_diffstat(worktree: Path, base_branch: str) -> str:
    """What changed, committed and not.

    Uncommitted work is included because an interrupted run still has something
    worth reporting — and because a run that edited files but never committed
    is a specific, recognisable failure that an empty diffstat would hide.
    """
    committed = diffstat(worktree, base_branch)
    pending = uncommitted_diffstat(worktree)
    if not pending.strip():
        return committed
    return f"{committed}\nUncommitted:\n{pending}".strip()


def _serialize_subtasks(proposals: list[SubtaskProposal] | None) -> dict | None:
    """A plan's proposed decomposition, in the shape the `proposed_subtasks`
    JSON column stores and the approve route later reads back."""
    if not proposals:
        return None
    return {
        "subtasks": [
            {"title": p.title, "body": p.body, "ready_to_execute": p.ready_to_execute}
            for p in proposals
        ]
    }


def record(db: Session, run: Run, ending: Ending) -> Run:
    """Translate how the agent ended into how the run ended.

    The plan phase stopping at `awaiting_review` rather than `succeeded` is the
    plan/execute split doing its job: the run is over, the task is not, and a
    person decides which.

    An execute run's *task*-level outcome comes from `run.agent_outcome`
    (written live, mid-run, through the outcome API) rather than from whether
    the backend process merely exited without crashing — see the module
    docstring on `runs.store.report_outcome`.
    """
    task = run.task
    base_branch = origin_branch_for(task)
    worktree = Path(task.worktree_path) if task.worktree_path else None

    match ending:
        case AgentFinished() if run.phase is RunPhase.PLAN:
            return finish_run(
                db,
                run,
                RunStatus.AWAITING_REVIEW,
                plan=ending.text,
                resume_token=ending.resume_token,
                model=ending.model,
                total_cost_usd=ending.total_cost_usd,
                num_turns=ending.num_turns,
                proposed_subtasks=_serialize_subtasks(ending.proposed_subtasks),
            )

        case AgentFinished() if run.agent_outcome is RunOutcome.NEEDS_REPLANNING:
            task.status = TaskStatus.BLOCKED
            return finish_run(
                db,
                run,
                RunStatus.AWAITING_REVIEW,
                summary=ending.text,
                diffstat=_worktree_diffstat(worktree, base_branch) if worktree else None,
                resume_token=ending.resume_token,
                model=ending.model,
                total_cost_usd=ending.total_cost_usd,
                num_turns=ending.num_turns,
            )

        case AgentFinished() if run.agent_outcome is RunOutcome.FAILED:
            task.status = TaskStatus.BLOCKED
            return finish_run(
                db,
                run,
                RunStatus.FAILED,
                summary=ending.text,
                error=run.outcome_detail or "The agent reported that this task failed.",
                diffstat=_worktree_diffstat(worktree, base_branch) if worktree else None,
                resume_token=ending.resume_token,
                model=ending.model,
                total_cost_usd=ending.total_cost_usd,
                num_turns=ending.num_turns,
            )

        case AgentFinished():
            # Either explicitly "finished", or the agent never called the
            # outcome API at all. Treating every clean process exit as
            # success would auto-close a task the agent silently left
            # unfinished — hitting the turn limit still yields AgentFinished
            # — so DONE requires an explicit report, and a stopped-early
            # report is distrusted even when it says "finished".
            if run.agent_outcome is RunOutcome.FINISHED and not ending.stopped_early:
                task.status = TaskStatus.DONE
            elif run.agent_outcome is RunOutcome.FINISHED:
                append_event(
                    db,
                    run.id,
                    RunEventKind.NOTICE,
                    {
                        "text": "The agent reported finishing, but the run was cut off "
                        "first (e.g. it ran out of turns) — the task's status was "
                        "left unchanged."
                    },
                )
            elif run.agent_outcome is None:
                append_event(
                    db,
                    run.id,
                    RunEventKind.NOTICE,
                    {
                        "text": "The agent did not report an outcome — the task's "
                        "status was left unchanged."
                    },
                )
            return finish_run(
                db,
                run,
                RunStatus.SUCCEEDED,
                summary=ending.text,
                diffstat=_worktree_diffstat(worktree, base_branch) if worktree else None,
                resume_token=ending.resume_token,
                model=ending.model,
                total_cost_usd=ending.total_cost_usd,
                num_turns=ending.num_turns,
            )

        case AgentFailed():
            return finish_run(
                db,
                run,
                RunStatus.FAILED,
                error=ending.message,
                diffstat=_worktree_diffstat(worktree, base_branch) if worktree else None,
                resume_token=ending.resume_token,
                model=ending.model,
                total_cost_usd=ending.total_cost_usd,
                num_turns=ending.num_turns,
            )

        case AgentUnavailable():
            return finish_run(db, run, RunStatus.FAILED, error=ending.message)

        case Interrupted():
            return finish_run(
                db,
                run,
                RunStatus.CANCELLED,
                error=f"Stopped by {ending.signal_name} before the agent finished.",
                diffstat=_worktree_diffstat(worktree, base_branch) if worktree else None,
            )


def execute(db: Session, run: Run) -> Run:
    """One run, start to finish, always ending in a recorded outcome."""
    mark_running(db, run)
    append_event(
        db,
        run.id,
        RunEventKind.NOTICE,
        {"text": f"Backend {run.backend}, billing {billing_mode()}."},
    )

    prepared = prepare(db, run)
    if isinstance(prepared, NotPrepared):
        return finish_run(db, run, RunStatus.FAILED, error=prepared.message)

    try:
        ending = asyncio.run(drive(db, run.id, prepared))
    except Exception as exc:
        # Nothing above should raise; a backend returns its failures. If one
        # does anyway, the run must still stop being `running` — an unrecorded
        # run holds a concurrency slot forever and explains nothing.
        logger.exception("Run %s crashed.", run.id)
        return finish_run(db, run, RunStatus.FAILED, error=f"The runner crashed: {exc}")

    return record(db, run, ending)


def main(argv: list[str] | None = None) -> int:
    """Entry point. One argument: the id of a run row that already exists.

    Applying the agent environment here, once, is what makes the billing
    decision in `config.py` real. Every backend inherits this process's
    environment, so stripping metered-API credentials at the single point they
    all pass through beats asking each backend to remember.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].isdigit():
        logger.error("Usage: python -m workbench.runs.runner <run_id>")
        return 2

    # Compute the policy against the environment as it stands, then apply only
    # what it removed. Clearing first and rebuilding is the obvious spelling
    # and is wrong: `agent_environment()` reads `os.environ`, so it would see
    # an empty one and hand back nothing — taking HOME with it, which under a
    # subscription is exactly where the credential lives.
    permitted = agent_environment()
    for name in set(os.environ) - set(permitted):
        del os.environ[name]
    os.environ.update(permitted)

    run_id = int(args[0])
    with session_scope() as db:
        run = db.get(Run, run_id)
        if run is None:
            logger.error("No run with id %s.", run_id)
            return 1
        if run.status is not RunStatus.QUEUED:
            # Refusing rather than reconciling, as the deployer does. Two
            # processes driving one run would interleave their events and
            # both write an outcome.
            logger.error("Run %s is %s, not queued.", run_id, run.status.value)
            return 1
        execute(db, run)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
