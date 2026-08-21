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
    create_task,
    delete_task,
    set_status,
)
from workbench.tasks.tree import TaskNode, build_tree, flatten, would_create_cycle

__all__ = [
    "TaskNode",
    "TaskNotFound",
    "WrongProject",
    "build_tree",
    "create_task",
    "delete_task",
    "flatten",
    "set_status",
    "would_create_cycle",
]
