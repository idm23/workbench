"""Every write against the task tree, in one place.

The HTML forms and the JSON API both create, complete, and delete tasks. Two
implementations of "add a task" would drift — one would learn about sibling
ordering or worktree cleanup and the other would not — so both call these.

Results rather than exceptions, following the rest of the codebase: a caller
discriminates with `isinstance` and a type checker can prove it handled the
failure. Every failure here is an ordinary condition — a missing task, a parent
in another project — not something exceptional.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.database.models import Project, RunPhase, Task, TaskStatus
from workbench.git.worktrees import local_checkout, remove_worktree

#: How a task is named when it is itself the origin something else branches
#: from. Lives here, not in `tasks.origin`, because `origin` already imports
#: `branch_choices_for` from this module — the reverse import would cycle.
_TASK_ORIGIN_PREFIX = "task:"


@dataclass(frozen=True)
class TaskNotFound:
    """No task with that id. Rendered as a 404 either way."""

    task_id: int

    @property
    def message(self) -> str:
        return f"No task with id {self.task_id}."


@dataclass(frozen=True)
class WrongProject:
    """A parent that belongs to a different project.

    Its own result rather than a generic "not found" because it means the
    request was coherent but wrong, which is worth saying differently.
    """

    message: str


def create_task(
    db: Session,
    project: Project,
    title: str,
    body: str | None = None,
    parent_id: int | None = None,
) -> Task | WrongProject:
    """Add a task, optionally under a parent.

    Position is assigned within the sibling group rather than across the whole
    project, so a new sub-task lands under its siblings instead of jumping to
    the bottom of the tree.
    """
    if parent_id is not None:
        parent = db.get(Task, parent_id)
        if parent is None or parent.project_id != project.id:
            return WrongProject("That parent task does not belong to this project.")

    siblings = db.scalars(
        select(Task.position).where(Task.project_id == project.id, Task.parent_id == parent_id)
    ).all()
    next_position = (max(siblings) + 1) if siblings else 0

    task = Task(
        project_id=project.id,
        parent_id=parent_id,
        title=title.strip(),
        body=(body or "").strip() or None,
        position=next_position,
    )
    db.add(task)
    db.commit()
    return task


def set_status(db: Session, task: Task, status: TaskStatus) -> Task:
    task.status = status
    db.commit()
    return task


def delete_task(db: Session, task: Task) -> str:
    """Delete a task and everything under it. Returns the title, for the message.

    Children, runs, and events go by database cascade. Worktrees do not — they
    are directories on disk that only these rows name, so they are removed
    before the rows that name them.
    """
    project = task.project
    title = task.title

    checkout = local_checkout(project.owner, project.repo)
    if checkout is not None:
        for doomed in descendants(task):
            if doomed.worktree_path:
                remove_worktree(checkout, Path(doomed.worktree_path))

    db.delete(task)
    db.commit()
    return title


def descendants(task: Task) -> list[Task]:
    """The task and everything beneath it, depth-first."""
    collected = [task]
    for child in task.children:
        collected.extend(descendants(child))
    return collected


def branch_choices_for(task: Task) -> list[Task]:
    """Other tasks in the same tree that already have a branch to build on.

    A task that depends on unmerged sibling work should be able to start from
    that sibling's branch directly, rather than only from main or staging.
    Walks up to the root first because the sibling in question need not be
    directly related to `task` — only in the same tree.
    """
    root = task
    while root.parent is not None:
        root = root.parent
    return [other for other in descendants(root) if other.id != task.id and other.branch]


def task_origin_value(task: Task) -> str:
    """The form/API value that selects `task`'s own branch as an origin."""
    return f"{_TASK_ORIGIN_PREFIX}{task.id}"


def task_id_from_origin_value(value: str) -> int | None:
    """The task id named by `task_origin_value`, or None if `value` is not one."""
    suffix = value.removeprefix(_TASK_ORIGIN_PREFIX)
    if suffix == value or not suffix.isdigit():
        return None
    return int(suffix)


def create_subtask(
    db: Session,
    task: Task,
    title: str,
    body: str | None = None,
    ready_to_execute: bool = False,
) -> Task | WrongProject:
    """Add a child of `task`, from a plan's decomposition or an execute run's
    live spin-off — the two callers this exists for.

    Defaults the new task's origin to `task`'s own branch when it has one:
    a subtask spun off mid-plan or mid-execute is continuing that work, not
    starting fresh from main. `ready_to_execute` sets `entry_phase` so a
    fully-specified piece can skip its own planning pass.
    """
    created = create_task(db, task.project, title=title, body=body, parent_id=task.id)
    if isinstance(created, WrongProject):
        return created

    if ready_to_execute:
        created.entry_phase = RunPhase.EXECUTE
    if task.branch:
        created.origin_ref = task_origin_value(task)
    db.commit()
    return created
