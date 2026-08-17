"""Schema behaviour that is easy to get silently wrong.

SQLite ignores foreign keys unless a pragma turns them on, so the cascade in
the model definition is not self-evidently in force. These tests check the
constraints actually bite at runtime rather than just existing in the DDL.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from workbench.db import make_engine
from workbench.models import (
    Base,
    Project,
    Run,
    RunEvent,
    RunPhase,
    RunStatus,
    Task,
    TaskStatus,
    User,
)


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _project(user: User, url: str = "https://github.com/idm23/workbench") -> Project:
    return Project(user=user, owner="idm23", repo="workbench", github_url=url)


def test_user_owns_projects(session):
    user = User(name="ian")
    session.add(_project(user))
    session.commit()

    assert session.get(User, user.id).projects[0].repo == "workbench"


def test_created_at_is_populated_automatically(session):
    user = User(name="ian")
    session.add(user)
    session.commit()

    assert user.created_at is not None


def test_same_repo_twice_for_one_user_is_rejected(session):
    user = User(name="ian")
    session.add_all([_project(user), _project(user)])

    with pytest.raises(IntegrityError):
        session.commit()


def test_two_users_may_each_add_the_same_repo(session):
    session.add_all([_project(User(name="ian")), _project(User(name="jake"))])
    session.commit()

    assert session.query(Project).count() == 2


def test_duplicate_user_name_is_rejected(session):
    session.add_all([User(name="ian"), User(name="ian")])

    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_a_user_deletes_their_projects(session):
    user = User(name="ian")
    session.add(_project(user))
    session.commit()

    session.delete(user)
    session.commit()

    assert session.query(Project).count() == 0


def test_foreign_keys_are_enforced(session):
    """Guards the PRAGMA in db.py — without it SQLite accepts this silently."""
    session.add(
        Project(
            user_id=9999,
            owner="idm23",
            repo="workbench",
            github_url="https://github.com/idm23/workbench",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()


# --- Tasks, runs, and events -------------------------------------------------
#
# The chain is projects -> tasks -> runs -> run_events, cascading at every link.
# Deleting a project has to take a whole tree of tasks and their entire run
# history with it; getting that wrong leaves orphaned rows no page will show
# again, which is invisible until the database is opened by hand.


def _task(project: Project, title: str = "Do the thing", **kwargs) -> Task:
    return Task(project=project, title=title, **kwargs)


@pytest.fixture
def project(session) -> Project:
    project = _project(User(name="ian"))
    session.add(project)
    session.commit()
    return project


def test_task_defaults_to_open_at_the_top_level(session, project):
    task = _task(project)
    session.add(task)
    session.commit()

    assert task.status == TaskStatus.OPEN
    assert task.position == 0
    assert task.parent_id is None


def test_status_is_stored_as_its_value_not_its_member_name(session, project):
    """The database, the rendered HTML, and the CSS class all read `done`."""
    task = _task(project, status=TaskStatus.DONE)
    session.add(task)
    session.commit()

    stored = session.execute(
        text("SELECT status FROM tasks WHERE id = :id"), {"id": task.id}
    ).scalar_one()

    assert stored == "done"


def test_status_comes_back_as_an_enum(session, project):
    task = _task(project, status=TaskStatus.BLOCKED)
    session.add(task)
    session.commit()
    session.expire_all()

    reloaded = session.get(Task, task.id)
    assert reloaded is not None
    assert reloaded.status is TaskStatus.BLOCKED
    # And still equal to the plain string, which the templates rely on.
    assert reloaded.status == "blocked"


def test_deleting_a_task_deletes_its_children(session, project):
    parent = _task(project, "parent")
    session.add(parent)
    session.commit()
    session.add(Task(project=project, parent_id=parent.id, title="child"))
    session.commit()

    session.delete(parent)
    session.commit()

    assert session.query(Task).count() == 0


def test_deleting_a_project_deletes_its_whole_task_tree(session, project):
    parent = _task(project, "parent")
    session.add(parent)
    session.commit()
    session.add(Task(project=project, parent_id=parent.id, title="child"))
    session.commit()

    session.delete(project)
    session.commit()

    assert session.query(Task).count() == 0


def test_deleting_a_task_deletes_its_runs_and_their_events(session, project):
    task = _task(project)
    session.add(task)
    session.commit()
    run = Run(task_id=task.id, phase=RunPhase.PLAN)
    session.add(run)
    session.commit()
    session.add(RunEvent(run_id=run.id, seq=1, kind="text", payload={"text": "hi"}))
    session.commit()

    session.delete(task)
    session.commit()

    assert session.query(Run).count() == 0
    assert session.query(RunEvent).count() == 0


def test_run_starts_queued_and_unfinished(session, project):
    task = _task(project)
    session.add(task)
    session.commit()

    run = Run(task_id=task.id, phase=RunPhase.EXECUTE)
    session.add(run)
    session.commit()

    assert run.status == RunStatus.QUEUED
    assert run.finished_at is None
    assert run.created_at is not None


def test_event_payload_round_trips_as_a_dict(session, project):
    """The JSON column owns the encoding, so no call site should be parsing."""
    task = _task(project)
    session.add(task)
    session.commit()
    run = Run(task_id=task.id, phase=RunPhase.PLAN)
    session.add(run)
    session.commit()

    payload = {"name": "Bash", "input": {"command": "ls -la"}, "nested": [1, 2, {"a": True}]}
    session.add(RunEvent(run_id=run.id, seq=1, kind="tool_use", payload=payload))
    session.commit()
    session.expire_all()

    stored = session.query(RunEvent).one()
    assert stored.payload == payload
    assert stored.payload["input"]["command"] == "ls -la"


def test_one_sequence_number_per_run(session, project):
    """Two events sharing a seq would make SSE replay skip or repeat one."""
    task = _task(project)
    session.add(task)
    session.commit()
    run = Run(task_id=task.id, phase=RunPhase.PLAN)
    session.add(run)
    session.commit()

    session.add_all(
        [
            RunEvent(run_id=run.id, seq=1, kind="text", payload={}),
            RunEvent(run_id=run.id, seq=1, kind="text", payload={}),
        ]
    )

    with pytest.raises(IntegrityError):
        session.commit()


def test_two_runs_may_reuse_the_same_sequence_numbers(session, project):
    task = _task(project)
    session.add(task)
    session.commit()
    first = Run(task_id=task.id, phase=RunPhase.PLAN)
    second = Run(task_id=task.id, phase=RunPhase.EXECUTE)
    session.add_all([first, second])
    session.commit()

    session.add_all(
        [
            RunEvent(run_id=first.id, seq=1, kind="text", payload={}),
            RunEvent(run_id=second.id, seq=1, kind="text", payload={}),
        ]
    )
    session.commit()

    assert session.query(RunEvent).count() == 2


def test_a_task_cannot_belong_to_a_missing_project(session):
    session.add(Task(project_id=9999, title="orphan"))

    with pytest.raises(IntegrityError):
        session.commit()


# --- Run state ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        (RunStatus.QUEUED, False),
        (RunStatus.RUNNING, False),
        (RunStatus.AWAITING_REVIEW, False),
        (RunStatus.SUCCEEDED, True),
        (RunStatus.FAILED, True),
        (RunStatus.CANCELLED, True),
    ],
)
def test_terminal_states(status, terminal):
    """awaiting_review is paused, not finished — approving it starts the next phase."""
    assert status.is_terminal is terminal


@pytest.mark.parametrize(
    ("status", "active"),
    [
        (RunStatus.QUEUED, True),
        (RunStatus.RUNNING, True),
        (RunStatus.AWAITING_REVIEW, False),
        (RunStatus.SUCCEEDED, False),
    ],
)
def test_active_states_hold_a_concurrency_slot(status, active):
    """A run waiting on a person must not occupy a slot while it waits."""
    assert status.is_active is active
