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
from workbench.tasks.store import branch_choices_for, task_id_from_origin_value, task_origin_value

#: The two branches every project is assumed to have, mirroring this
#: project's own main/staging convention. Neither is looked up on GitHub:
#: "main" resolves through the project's own default branch, and "staging" is
#: asked for literally and fails like any other missing ref if a project
#: does not have one.
PROD = "main"
DEV = "staging"

#: What the picker offers first, and therefore what most runs branch from.
#:
#: Dev, because of where the work ends up rather than where it starts. A run's
#: pull request targets whatever its worktree was cut from, and this project
#: promotes dev into prod with a merge commit — so a branch cut from prod and
#: aimed at dev carries every promotion merge prod has had since, and the
#: pull request lists them as though they were part of the change.
#:
#: Deliberately not `resolve_origin`'s fallback for an *unset* origin, which
#: stays on prod: this is the default for a person choosing in the UI, on a
#: project they know has a dev branch, whereas an unset origin means a task
#: created through the JSON API or before any of this existed, on a project
#: that may well have nothing called `staging` at all.
DEFAULT = DEV


@dataclass(frozen=True)
class InvalidOrigin:
    """The submitted origin does not name anything this task can branch from."""

    message: str


def origin_choices(task: Task) -> list[tuple[str, str]]:
    """(value, label) pairs for the dropdown shown when starting a task's first run."""
    choices = [(DEV, "staging (dev)"), (PROD, "main (prod)")]
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
    other_id = task_id_from_origin_value(choice)
    if other_id is not None:
        other = next((t for t in branch_choices_for(task) if t.id == other_id), None)
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
