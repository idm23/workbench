"""Tree assembly.

The tree is the one piece of task handling with real logic in it — everything
else is a row in a table — and it is rendered as a flat list with indentation,
so a mistake in ordering or depth is silent rather than obvious.
"""

import pytest

from workbench.database.models import Task, TaskStatus
from workbench.tasks import build_tree, flatten, would_create_cycle


def make(task_id: int, parent: int | None = None, position: int = 0, title: str = "t") -> Task:
    """A Task detached from any session, which is all the tree code needs."""
    return Task(id=task_id, parent_id=parent, position=position, title=title, project_id=1)


def titles(nodes) -> list[str]:
    return [node.task.title for node in nodes]


def test_flat_list_becomes_roots():
    roots = build_tree([make(1, title="a"), make(2, title="b")])

    assert titles(roots) == ["a", "b"]
    assert all(node.depth == 0 for node in roots)


def test_children_nest_under_their_parent():
    roots = build_tree([make(1, title="parent"), make(2, parent=1, title="child")])

    assert len(roots) == 1
    assert titles(roots[0].children) == ["child"]
    assert roots[0].children[0].depth == 1


def test_siblings_order_by_position_then_id():
    roots = build_tree(
        [
            make(3, position=0, title="first"),
            make(1, position=1, title="second"),
            make(2, position=0, title="tie-break"),
        ]
    )

    # Position 0 twice, so the lower id wins the tie.
    assert titles(roots) == ["tie-break", "first", "second"]


def test_flatten_is_depth_first():
    tree = build_tree(
        [
            make(1, title="a"),
            make(2, parent=1, title="a1"),
            make(3, parent=2, title="a1i"),
            make(4, title="b"),
        ]
    )

    assert titles(flatten(tree)) == ["a", "a1", "a1i", "b"]
    assert [node.depth for node in flatten(tree)] == [0, 1, 2, 0]


def test_orphan_is_shown_as_a_root_rather_than_dropped():
    """A task whose parent is missing must not vanish from the page."""
    roots = build_tree([make(2, parent=99, title="orphan")])

    assert titles(roots) == ["orphan"]


def test_only_childless_tasks_are_leaves():
    roots = build_tree([make(1, title="parent"), make(2, parent=1, title="child")])

    assert not roots[0].is_leaf
    assert roots[0].children[0].is_leaf


def test_progress_counts_only_direct_children():
    tasks = [make(1, title="parent"), make(2, parent=1), make(3, parent=1)]
    tasks[1].status = TaskStatus.DONE

    roots = build_tree(tasks)

    assert roots[0].progress == "1/2"


def test_a_leaf_has_no_progress():
    assert build_tree([make(1)])[0].progress is None


def test_self_parenting_task_does_not_recurse():
    """Corrupt data must render, not hang."""
    roots = build_tree([make(1, parent=1, title="loop")])

    assert titles(roots) == ["loop"]


@pytest.mark.parametrize("same", [True, False])
def test_cycle_detection(same):
    parent = make(1)
    child = make(2, parent=1)
    child.parent = parent

    assert would_create_cycle(parent, parent if same else child)
