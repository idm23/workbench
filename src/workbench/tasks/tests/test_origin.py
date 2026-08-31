"""Resolving a task's chosen origin into a git ref.

Every branch here has a caller that already trusts it: the web route rejects
a bad choice before a run is even queued, and the runner trusts a stored one
enough to hand it straight to `ensure_worktree`. So an invalid choice has to
come back as data, not raise, in both directions.
"""

import pytest
from sqlalchemy.orm import Session

from workbench.database.db import make_engine
from workbench.database.models import Base, Project, Task, User
from workbench.tasks import create_task
from workbench.tasks.origin import (
    DEFAULT,
    DEV,
    PROD,
    InvalidOrigin,
    origin_branch_for,
    origin_choices,
    resolve_origin,
    task_origin_value,
)


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
        default_branch="trunk",
    )
    session.add(project)
    session.commit()
    return project


def test_unset_resolves_to_the_projects_default_branch(session, project):
    task = create_task(session, project, title="x")
    assert isinstance(task, Task)

    assert resolve_origin(task, None) == "trunk"
    assert resolve_origin(task, "") == "trunk"


def test_prod_falls_back_to_main_with_no_default_branch(session):
    project = Project(
        user=User(name="ian"),
        owner="idm23",
        repo="workbench",
        github_url="https://github.com/idm23/workbench",
    )
    session.add(project)
    session.commit()
    task = create_task(session, project, title="x")
    assert isinstance(task, Task)

    assert resolve_origin(task, PROD) == "main"


def test_dev_resolves_to_the_literal_staging_branch(session, project):
    task = create_task(session, project, title="x")
    assert isinstance(task, Task)

    assert resolve_origin(task, DEV) == "staging"


def test_a_sibling_branch_is_selectable_by_task_id(session, project):
    parent = create_task(session, project, title="parent")
    assert isinstance(parent, Task)
    sibling = create_task(session, project, title="sibling", parent_id=parent.id)
    task = create_task(session, project, title="task", parent_id=parent.id)
    assert isinstance(sibling, Task) and isinstance(task, Task)
    sibling.branch = "workbench/task-2-sibling"
    session.commit()

    assert resolve_origin(task, task_origin_value(sibling)) == "workbench/task-2-sibling"


def test_an_unbranched_sibling_is_not_a_valid_origin(session, project):
    parent = create_task(session, project, title="parent")
    assert isinstance(parent, Task)
    sibling = create_task(session, project, title="sibling", parent_id=parent.id)
    task = create_task(session, project, title="task", parent_id=parent.id)
    assert isinstance(sibling, Task) and isinstance(task, Task)

    result = resolve_origin(task, task_origin_value(sibling))

    assert isinstance(result, InvalidOrigin)


def test_a_task_in_another_tree_is_not_a_valid_origin(session, project):
    tree_a = create_task(session, project, title="tree a")
    assert isinstance(tree_a, Task)
    task = create_task(session, project, title="task", parent_id=tree_a.id)
    tree_b = create_task(session, project, title="tree b")
    assert isinstance(tree_b, Task)
    stranger = create_task(session, project, title="stranger", parent_id=tree_b.id)
    assert isinstance(task, Task) and isinstance(stranger, Task)
    stranger.branch = "workbench/task-4-stranger"
    session.commit()

    result = resolve_origin(task, task_origin_value(stranger))

    assert isinstance(result, InvalidOrigin)


def test_garbage_is_rejected_readably(session, project):
    task = create_task(session, project, title="x")
    assert isinstance(task, Task)

    result = resolve_origin(task, "not-a-real-choice")

    assert isinstance(result, InvalidOrigin)
    assert "not-a-real-choice" in result.message


def test_origin_choices_always_offers_main_and_staging(session, project):
    task = create_task(session, project, title="x")
    assert isinstance(task, Task)

    assert origin_choices(task)[:2] == [(DEV, "staging (dev)"), (PROD, "main (prod)")]


def test_dev_is_offered_first_because_that_is_where_the_pull_request_goes(session, project):
    """Order is the default: the picker preselects the first option for a task
    nobody has chosen one for, and a run's pull request targets whatever its
    worktree was cut from. Branching from prod would aim the pull request at
    prod and carry every promotion merge since into its commit list."""
    task = create_task(session, project, title="x")
    assert isinstance(task, Task)

    assert origin_choices(task)[0][0] == DEFAULT == DEV


def test_an_unset_origin_still_falls_back_to_prod(session, project):
    """Deliberately not DEFAULT. An unset origin means a task created through
    the JSON API or before any of this existed, on a project that may have no
    branch called `staging` at all — where prod is the only ref known to
    resolve."""
    task = create_task(session, project, title="x")
    assert isinstance(task, Task)

    assert resolve_origin(task, None) == project.default_branch


def test_origin_choices_adds_a_branched_task_in_the_same_tree(session, project):
    parent = create_task(session, project, title="parent")
    assert isinstance(parent, Task)
    sibling = create_task(session, project, title="sibling", parent_id=parent.id)
    task = create_task(session, project, title="task", parent_id=parent.id)
    assert isinstance(sibling, Task) and isinstance(task, Task)
    sibling.branch = "workbench/task-2-sibling"
    session.commit()

    choices = origin_choices(task)

    assert (task_origin_value(sibling), "sibling (workbench/task-2-sibling)") in choices


def test_origin_branch_for_falls_back_when_the_stored_choice_no_longer_resolves(session, project):
    """A diffstat read back later should not go blank over a stale choice."""
    task = create_task(session, project, title="x")
    assert isinstance(task, Task)
    task.origin_ref = "task:9999"
    session.commit()

    assert origin_branch_for(task) == "trunk"
