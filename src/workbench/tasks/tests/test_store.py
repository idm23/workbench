"""The shared write operations.

These exist so the HTML forms and the JSON API cannot drift, which makes their
behaviour worth pinning here rather than in either caller's tests.
"""

import pytest
from sqlalchemy.orm import Session

from workbench.database.db import make_engine
from workbench.database.models import Base, Project, Task, TaskStatus, User
from workbench.tasks import WrongProject, create_task, delete_task, set_status
from workbench.tasks.store import branch_choices_for, descendants


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "test.db"))
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def project(session) -> Project:
    project = Project(
        user=User(name="ian"),
        owner="idm23",
        repo="workbench",
        github_url="https://github.com/idm23/workbench",
    )
    session.add(project)
    session.commit()
    return project


def test_a_new_task_starts_open_at_the_top(session, project):
    task = create_task(session, project, title="Do the thing")

    assert isinstance(task, Task)
    assert task.status is TaskStatus.OPEN
    assert task.parent_id is None
    assert task.position == 0


def test_titles_and_bodies_are_trimmed(session, project):
    task = create_task(session, project, title="  padded  ", body="  detail  ")

    assert isinstance(task, Task)
    assert task.title == "padded"
    assert task.body == "detail"


def test_an_empty_body_is_stored_as_null(session, project):
    """So the template can ask `if task.body` rather than comparing strings."""
    task = create_task(session, project, title="No detail", body="   ")

    assert isinstance(task, Task)
    assert task.body is None


def test_position_increments_within_the_sibling_group(session, project):
    first = create_task(session, project, title="first")
    second = create_task(session, project, title="second")
    assert isinstance(first, Task) and isinstance(second, Task)

    assert [first.position, second.position] == [0, 1]


def test_a_subtask_is_positioned_among_its_siblings_not_the_project(session, project):
    """Otherwise a new sub-task jumps to the bottom of the whole tree."""
    parent = create_task(session, project, title="parent")
    create_task(session, project, title="noise")
    create_task(session, project, title="more noise")
    assert isinstance(parent, Task)

    child = create_task(session, project, title="child", parent_id=parent.id)

    assert isinstance(child, Task)
    assert child.position == 0


def test_a_parent_from_another_project_is_refused(session, project):
    other = Project(
        user=User(name="jake"),
        owner="idm23",
        repo="other",
        github_url="https://github.com/idm23/other",
    )
    session.add(other)
    session.commit()
    stranger = create_task(session, other, title="theirs")
    assert isinstance(stranger, Task)

    result = create_task(session, project, title="mine", parent_id=stranger.id)

    assert isinstance(result, WrongProject)


def test_a_missing_parent_is_refused(session, project):
    assert isinstance(create_task(session, project, title="x", parent_id=9999), WrongProject)


def test_status_changes_persist(session, project):
    task = create_task(session, project, title="x")
    assert isinstance(task, Task)

    set_status(session, task, TaskStatus.DONE)
    session.expire_all()

    reloaded = session.get(Task, task.id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.DONE


def test_deleting_returns_the_title_for_the_message(session, project):
    task = create_task(session, project, title="Ship it")
    assert isinstance(task, Task)

    assert delete_task(session, task) == "Ship it"


def test_deleting_takes_the_children(session, project):
    parent = create_task(session, project, title="parent")
    assert isinstance(parent, Task)
    create_task(session, project, title="child", parent_id=parent.id)

    delete_task(session, parent)

    assert session.query(Task).count() == 0


def test_descendants_is_depth_first_and_includes_the_task(session, project):
    parent = create_task(session, project, title="parent")
    assert isinstance(parent, Task)
    child = create_task(session, project, title="child", parent_id=parent.id)
    assert isinstance(child, Task)
    create_task(session, project, title="grandchild", parent_id=child.id)
    session.refresh(parent)

    assert [task.title for task in descendants(parent)] == ["parent", "child", "grandchild"]


# --- Branch choices for the origin picker -----------------------------------


def test_branch_choices_finds_a_branched_sibling(session, project):
    parent = create_task(session, project, title="parent")
    assert isinstance(parent, Task)
    sibling = create_task(session, project, title="sibling", parent_id=parent.id)
    other = create_task(session, project, title="other", parent_id=parent.id)
    assert isinstance(sibling, Task) and isinstance(other, Task)
    other.branch = "workbench/task-2-other"
    session.commit()

    assert branch_choices_for(sibling) == [other]


def test_branch_choices_excludes_the_task_itself(session, project):
    task = create_task(session, project, title="solo")
    assert isinstance(task, Task)
    task.branch = "workbench/task-1-solo"
    session.commit()

    assert branch_choices_for(task) == []


def test_branch_choices_excludes_unbranched_tasks(session, project):
    parent = create_task(session, project, title="parent")
    assert isinstance(parent, Task)
    sibling = create_task(session, project, title="sibling", parent_id=parent.id)
    create_task(session, project, title="never run", parent_id=parent.id)
    assert isinstance(sibling, Task)

    assert branch_choices_for(sibling) == []


def test_branch_choices_ignores_a_different_tree(session, project):
    tree_a = create_task(session, project, title="tree a")
    assert isinstance(tree_a, Task)
    task = create_task(session, project, title="task in tree a", parent_id=tree_a.id)
    tree_b = create_task(session, project, title="tree b")
    assert isinstance(tree_b, Task)
    other_tree_task = create_task(session, project, title="task in tree b", parent_id=tree_b.id)
    assert isinstance(task, Task) and isinstance(other_tree_task, Task)
    other_tree_task.branch = "workbench/task-4-task-in-tree-b"
    session.commit()

    assert branch_choices_for(task) == []
