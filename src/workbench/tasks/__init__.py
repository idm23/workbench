"""Tasks: the tree they form, and the operations that change them.

`tree` is pure — it turns rows into the shape a page renders and touches no
database, so ordering and nesting can be tested directly. `store` is the
opposite half: every write, in one place, because the HTML forms and the JSON
API both perform them and two copies would drift.

The names below are re-exported so callers say `from workbench.tasks import
build_tree` without caring which half it came from.
"""

from workbench.tasks.store import (
    TaskNotFound,
    WrongProject,
    archive_task,
    branch_choices_for,
    create_subtask,
    create_task,
    delete_task,
    set_status,
    unarchive_task,
)
from workbench.tasks.tree import (
    ReadyTask,
    ReviewTask,
    TaskNode,
    build_tree,
    flatten,
    ready_for_review,
    ready_to_execute,
    would_create_cycle,
)

__all__ = [
    "ReadyTask",
    "ReviewTask",
    "TaskNode",
    "TaskNotFound",
    "WrongProject",
    "archive_task",
    "branch_choices_for",
    "build_tree",
    "create_subtask",
    "create_task",
    "delete_task",
    "flatten",
    "ready_for_review",
    "ready_to_execute",
    "set_status",
    "unarchive_task",
    "would_create_cycle",
]
