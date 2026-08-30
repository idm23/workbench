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

from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.database.models import Run, RunPhase, RunStatus, Task

#: Statuses worth marking. The two active ones because work is happening, and
#: `awaiting_review` because a plan nobody has looked at is the state most
#: likely to be forgotten — it is waiting on a person, and nothing else on the
#: page says so.
MARKED_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.AWAITING_REVIEW)


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
        return self.status is RunStatus.AWAITING_REVIEW


def activity_by_task(db: Session, project_id: int) -> dict[int, TaskActivity]:
    """The run worth marking on each of a project's tasks, keyed by task id.

    A task can accumulate several runs — a plan, then an execute, then a retry
    — so where more than one qualifies the newest wins, which is the one whose
    state the page is describing.
    """
    rows = db.execute(
        select(Run.id, Run.task_id, Run.phase, Run.status, Run.proposed_subtasks)
        .join(Task, Task.id == Run.task_id)
        .where(Task.project_id == project_id, Run.status.in_(MARKED_STATUSES))
        .order_by(Run.id)
    ).all()

    # Ascending, so a later row overwrites an earlier one and the newest wins.
    return {
        task_id: TaskActivity(
            run_id=run_id, phase=phase, status=status, proposed_subtasks=proposed_subtasks
        )
        for run_id, task_id, phase, status, proposed_subtasks in rows
    }
