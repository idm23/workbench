"""Tree assembly.

The tree is the one piece of task handling with real logic in it — everything
else is a row in a table — and it is rendered as a flat list with indentation,
so a mistake in ordering or depth is silent rather than obvious.
"""

import pytest

from workbench.database.models import RunPhase, RunStatus, Task, TaskStatus
from workbench.runs.activity import TaskActivity
from workbench.tasks import build_tree, flatten, ready_for_review, would_create_cycle


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


def test_progress_percent_matches_the_fraction():
    tasks = [make(1, title="parent"), make(2, parent=1), make(3, parent=1), make(4, parent=1)]
    tasks[1].status = TaskStatus.DONE

    roots = build_tree(tasks)

    assert roots[0].progress_percent == 33


def test_a_leaf_has_no_progress_percent():
    assert build_tree([make(1)])[0].progress_percent is None


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


# --- Deriving a parent's status from its children ---------------------------


def test_a_leaf_reports_its_own_status():
    task = make(1)
    task.status = TaskStatus.BLOCKED

    assert build_tree([task])[0].effective_status is TaskStatus.BLOCKED


def test_a_parent_is_active_once_it_has_children():
    """The moment a plan decomposes a task, it reads as in-progress — not
    only once a child is picked up."""
    tasks = [make(1, title="parent"), make(2, parent=1)]
    tasks[0].status = TaskStatus.OPEN
    tasks[1].status = TaskStatus.OPEN

    assert build_tree(tasks)[0].effective_status is TaskStatus.ACTIVE


def test_a_parent_is_done_once_every_child_is():
    tasks = [make(1, title="parent"), make(2, parent=1), make(3, parent=1)]
    tasks[0].status = TaskStatus.OPEN
    tasks[1].status = TaskStatus.DONE
    tasks[2].status = TaskStatus.DONE

    assert build_tree(tasks)[0].effective_status is TaskStatus.DONE


def test_a_parent_is_blocked_if_any_child_is_even_when_others_are_done():
    tasks = [make(1, title="parent"), make(2, parent=1), make(3, parent=1)]
    tasks[0].status = TaskStatus.OPEN
    tasks[1].status = TaskStatus.DONE
    tasks[2].status = TaskStatus.BLOCKED

    assert build_tree(tasks)[0].effective_status is TaskStatus.BLOCKED


def test_a_manually_set_terminal_status_overrides_the_children():
    """A person's own word — done or cancelled — is respected regardless of
    what the children say, and nothing here writes it back."""
    tasks = [make(1, title="parent"), make(2, parent=1)]
    tasks[0].status = TaskStatus.CANCELLED
    tasks[1].status = TaskStatus.OPEN

    assert build_tree(tasks)[0].effective_status is TaskStatus.CANCELLED


def test_effective_status_derives_recursively():
    """A grandparent's status follows a parent's *derived* status, not its
    raw stored one."""
    tasks = [
        make(1, title="grandparent"),
        make(2, parent=1, title="parent"),
        make(3, parent=2, title="child"),
    ]
    tasks[0].status = TaskStatus.OPEN
    tasks[1].status = TaskStatus.OPEN
    tasks[2].status = TaskStatus.BLOCKED

    grandparent = build_tree(tasks)[0]
    assert grandparent.children[0].effective_status is TaskStatus.BLOCKED
    assert grandparent.effective_status is TaskStatus.BLOCKED


def test_done_count_uses_effective_status_not_raw_status():
    """A nested parent counts toward progress once its own children are all
    done, even though nothing ever set its own stored status to done."""
    tasks = [
        make(1, title="parent"),
        make(2, parent=1, title="child-parent"),
        make(3, parent=2, title="grandchild"),
    ]
    tasks[0].status = TaskStatus.OPEN
    tasks[1].status = TaskStatus.OPEN  # never marked done directly
    tasks[2].status = TaskStatus.DONE  # but its own child is

    roots = build_tree(tasks)
    assert roots[0].children[0].effective_status is TaskStatus.DONE
    assert roots[0].progress == "1/1"


def busy(run_id: int = 1) -> TaskActivity:
    """A minimal in-flight run, for tasks that should be excluded as busy."""
    return TaskActivity(run_id=run_id, phase=RunPhase.CONVERSATION, status=RunStatus.RUNNING)


@pytest.mark.parametrize("status", [TaskStatus.OPEN, TaskStatus.ACTIVE, TaskStatus.BLOCKED])
def test_a_leaf_with_an_open_pr_is_ready_for_review(status):
    task = make(1, title="leaf")
    task.status = status
    nodes = flatten(build_tree([task]))

    result = ready_for_review(nodes, activity={}, pr_urls={1: "https://github.com/x/y/pull/1"})

    assert [item.node.task.id for item in result] == [1]
    assert result[0].pr_url == "https://github.com/x/y/pull/1"


@pytest.mark.parametrize("status", [TaskStatus.DONE, TaskStatus.CANCELLED])
def test_a_finished_task_is_not_ready_for_review_even_with_an_open_pr(status):
    task = make(1, title="leaf")
    task.status = status
    nodes = flatten(build_tree([task]))

    result = ready_for_review(nodes, activity={}, pr_urls={1: "https://github.com/x/y/pull/1"})

    assert result == []


def test_a_leaf_with_no_pr_is_not_ready_for_review():
    nodes = flatten(build_tree([make(1, title="leaf")]))

    result = ready_for_review(nodes, activity={}, pr_urls={})

    assert result == []


def test_a_task_with_something_running_against_it_is_not_ready_for_review():
    """An open PR with a follow-up run in flight is left to the main tree row,
    which already shows the busy badge and a way to stop it."""
    nodes = flatten(build_tree([make(1, title="leaf")]))

    result = ready_for_review(
        nodes, activity={1: busy()}, pr_urls={1: "https://github.com/x/y/pull/1"}
    )

    assert result == []


def test_a_parent_task_is_never_ready_for_review():
    """Only leaves ever run agents, so only leaves ever earn a `pr_url` — but
    the check is defensive, mirroring `ready_to_execute`'s own leaf guard."""
    tasks = [make(1, title="parent"), make(2, parent=1, title="child")]
    nodes = flatten(build_tree(tasks))

    result = ready_for_review(nodes, activity={}, pr_urls={1: "https://github.com/x/y/pull/1"})

    assert result == []
