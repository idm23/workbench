"""Which tasks have something happening to them right now.

The task tree is the page someone opens on a phone to see where things stand,
and a task an agent is presently working is the one thing on it that changes
without anyone touching the page. It needs to be visible at a glance rather
than inferred from a branch name appearing.

One query for the whole tree rather than a run lookup per node: the tree is
rendered as a flat list of up to a few dozen tasks, and doing this per row is
the classic way a page that felt instant stops being one.
"""

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from workbench.database.models import Run, RunEvent, RunPhase, RunStatus, Task

#: Statuses worth marking. The two active ones because work is happening,
#: `awaiting_review` because a plan nobody has looked at is the state most
#: likely to be forgotten — it is waiting on a person, and nothing else on the
#: page says so — and `failed` for the same reason from the other direction: a
#: run that failed for a reason that has nothing to do with the work itself
#: (a rate-limit window, a dropped connection) still has a resume token worth
#: not losing, and the button to use it should be right where the run was.
#:
#: Deliberately applied *after* picking a task's newest run rather than as a
#: filter on the query — see `activity_by_task`.
MARKED_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.AWAITING_REVIEW, RunStatus.FAILED)


@dataclass(frozen=True)
class TaskActivity:
    """What is happening to one task, in the terms the page shows."""

    run_id: int
    phase: RunPhase
    status: RunStatus
    #: A plan run's proposed decomposition, when there is one. Carried here
    #: rather than fetched separately so the page can decide "Execute" vs
    #: "Approve & create N subtasks" without a second query per row.
    proposed_subtasks: dict | None = None
    #: A plan run's own output text, when it has finished one. Carried here for
    #: the same reason as `proposed_subtasks`: the "ready to execute" summary
    #: at the top of the page needs to show it, and this is the query that
    #: already visits every marked run once per page load.
    plan: str | None = None

    @property
    def proposed_subtask_count(self) -> int:
        return len((self.proposed_subtasks or {}).get("subtasks", []))

    @property
    def label(self) -> str:
        """Short enough for a badge, and about the work rather than the row.

        "Planning" and "working" rather than "running (plan)" because the
        phase is the part a person cares about; that a row's status column
        says `running` either way is Workbench's bookkeeping, not theirs.
        """
        if self.status is RunStatus.QUEUED:
            return "queued"
        if self.status is RunStatus.AWAITING_REVIEW:
            return "review"
        if self.status is RunStatus.FAILED:
            return "failed"
        return "planning" if self.phase is RunPhase.PLAN else "working"

    @property
    def is_live(self) -> bool:
        """Whether the agent is actually doing something at this moment.

        Drives the animated marker, and only this. A queued run and a plan
        waiting on a person are both stationary, and animating them would spend
        the reader's attention on things that are not moving.
        """
        return self.status is RunStatus.RUNNING

    @property
    def needs_attention(self) -> bool:
        """Specifically: there is a plan waiting to be approved.

        `failed` also wants a person, but a different action (retry, not
        approve) — kept as its own check on `status` at the call site rather
        than folded in here, so this keeps meaning exactly one thing.
        """
        return self.status is RunStatus.AWAITING_REVIEW


def activity_by_task(db: Session, project_id: int) -> dict[int, TaskActivity]:
    """The run worth marking on each of a project's tasks, keyed by task id.

    A task can accumulate several runs — a plan, then an execute, then a retry
    — so the newest wins, which is the one whose state the page is describing.

    "Newest" is taken across *every* run of the task, and only then judged
    worth marking. Filtering to `MARKED_STATUSES` first would be the same
    thing right up until a run finished cleanly: `succeeded` is not marked, so
    an older `awaiting_review` would survive it and go on describing a task
    whose work is long done. That is not hypothetical — a plan run left the
    tree offering to approve it after two later runs had carried it out,
    opened a pull request, and marked the task done, so every press started
    another agent against finished work and nothing on the page ever moved.
    """
    rows = db.execute(
        select(Run.id, Run.task_id, Run.phase, Run.status, Run.proposed_subtasks, Run.plan)
        .join(Task, Task.id == Run.task_id)
        .where(Task.project_id == project_id)
        .order_by(Run.id)
    ).all()

    # Ascending, so a later row overwrites an earlier one and the newest wins.
    newest = {
        task_id: TaskActivity(
            run_id=run_id,
            phase=phase,
            status=status,
            proposed_subtasks=proposed_subtasks,
            plan=plan,
        )
        for run_id, task_id, phase, status, proposed_subtasks, plan in rows
    }

    return {
        task_id: activity
        for task_id, activity in newest.items()
        if activity.status in MARKED_STATUSES
    }


def pr_url_by_task(db: Session, project_id: int) -> dict[int, str]:
    """The most recent pull request opened for each of a project's tasks.

    `run_detail.html` already shows a run's own `pr_url`, but that means
    digging into the specific run that opened it. This is what lets the task
    tree itself link straight to the code — the actual "menu action" a
    finished task is missing. A task can accumulate several runs, and where
    more than one opened a pull request the newest wins, exactly like
    `activity_by_task` above; one query for the whole tree for the same
    reason.
    """
    rows = db.execute(
        select(Run.task_id, Run.pr_url)
        .join(Task, Task.id == Run.task_id)
        .where(Task.project_id == project_id, Run.pr_url.is_not(None))
        .order_by(Run.id)
    ).all()

    return {task_id: pr_url for task_id, pr_url in rows}  # noqa: C416


def project_activity_fingerprint(db: Session, project_id: int) -> str:
    """An opaque marker of "has anything on this project's page changed".

    Polled by `project_detail.html` against `/projects/{id}/activity-version`
    so the tree can reload itself when a run transitions or a task changes on
    another device — never parsed, only compared for equality against the
    value the page rendered with, so what it is made of matters only here.

    No single column moves on every kind of change that page cares about, so
    this concatenates two aggregates:

    - `COUNT`/`MAX(updated_at)` over the project's tasks. `COUNT` catches a
      task being added or deleted, neither of which bumps any surviving row's
      `updated_at`; `MAX(updated_at)` catches an edit or a status toggle to a
      task that already existed. Together they catch everything a `Task` row
      can do without needing to diff the tree itself.
    - `MAX(RunEvent.id)` over every run under this project — through a task,
      or standing directly on it as the project's own conversation (see
      `Run.project_id`). Every run lifecycle transition (queued -> running ->
      succeeded/failed/awaiting_review), a pull request opening, and a
      rate-limit notice are all already written as a `run_events` row, so the
      newest event id already tracks all of it without reasoning about
      `Run`'s own columns — which has no `updated_at` to read instead.
    """
    task_count, task_max_updated = db.execute(
        select(func.count(Task.id), func.max(Task.updated_at)).where(Task.project_id == project_id)
    ).one()

    event_max_id = db.scalar(
        select(func.max(RunEvent.id))
        .select_from(RunEvent)
        .join(Run, Run.id == RunEvent.run_id)
        .outerjoin(Task, Task.id == Run.task_id)
        .where(or_(Task.project_id == project_id, Run.project_id == project_id))
    )

    return f"{task_count}:{task_max_updated}:{event_max_id}"
