"""Resolving what a task's worktree should be branched from.

A task's `origin_ref` only records the *choice* someone made when starting
its first run — "main", "staging", or another task in the same tree — never
an actual git ref. Resolving it happens twice: once when the choice is made,
so a bad one is rejected before a run is even queued, and again in the
runner, because the choice was made minutes or days earlier and has to be
checked against the tree as it exists now, not as it existed then.
"""

from dataclasses import dataclass

from workbench.database.models import Task
from workbench.tasks.store import branch_choices_for

#: The two branches every project is assumed to have, mirroring this
#: project's own main/staging convention. Neither is looked up on GitHub:
#: "main" resolves through the project's own default branch, and "staging" is
#: asked for literally and fails like any other missing ref if a project
#: does not have one.
PROD = "main"
DEV = "staging"

_TASK_PREFIX = "task:"


@dataclass(frozen=True)
class InvalidOrigin:
    """The submitted origin does not name anything this task can branch from."""

    message: str


def task_origin_value(other: Task) -> str:
    """The form value that selects another task's branch as the origin."""
    return f"{_TASK_PREFIX}{other.id}"


def origin_choices(task: Task) -> list[tuple[str, str]]:
    """(value, label) pairs for the dropdown shown when starting a task's first run."""
    choices = [(PROD, "main (prod)"), (DEV, "staging (dev)")]
    choices += [
        (task_origin_value(other), f"{other.title} ({other.branch})")
        for other in branch_choices_for(task)
    ]
    return choices


def resolve_origin(task: Task, origin_ref: str | None) -> str | InvalidOrigin:
    """The actual ref `ensure_worktree` should branch from.

    Unset resolves to prod — the only sane default for a task nobody has
    chosen one for yet, such as one created through the JSON API or before
    this existed.
    """
    choice = origin_ref or PROD

    if choice == PROD:
        return task.project.default_branch or "main"
    if choice == DEV:
        return DEV
    if choice.startswith(_TASK_PREFIX):
        other_id = choice.removeprefix(_TASK_PREFIX)
        if other_id.isdigit():
            other = next((t for t in branch_choices_for(task) if t.id == int(other_id)), None)
            # `branch_choices_for` only ever returns tasks with a branch, but
            # that is a runtime invariant a type checker cannot see through.
            if other is not None and other.branch is not None:
                return other.branch

    return InvalidOrigin(f"{origin_ref!r} is not a valid origin for this task.")


def origin_branch_for(task: Task) -> str:
    """The ref a task's branch actually diverged from, falling back rather than failing.

    Read long after the worktree was created, purely to render a diffstat —
    a since-renamed branch or a deleted origin task should not blank that out.
    """
    resolved = resolve_origin(task, task.origin_ref)
    return resolved if isinstance(resolved, str) else (task.project.default_branch or "main")
