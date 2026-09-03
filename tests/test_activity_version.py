"""The fingerprint that lets the task tree notice it has gone stale.

`project_activity_fingerprint` is the one thing the auto-refresh poll in
`project_detail.html` trusts: it has to move on everything that would make the
rendered page wrong (a task added or removed, a task edited, a run's status
moving on) and hold still on everything that would not (someone else's
project changing). These tests exercise the function directly, plus the thin
`/projects/{id}/activity-version` route that serves it to the poll script.
"""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from workbench.app import app
from workbench.database.db import get_db, make_engine
from workbench.database.models import (
    Base,
    Project,
    Run,
    RunEvent,
    RunEventKind,
    RunPhase,
    RunStatus,
    Task,
    TaskStatus,
    User,
)
from workbench.runs.activity import project_activity_fingerprint


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "test.db"))
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)

    def override():
        db = Session(engine)
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with Session(engine) as db:
        project = Project(
            user=User(name="ian"),
            owner="idm23",
            repo="workbench",
            github_url="https://github.com/idm23/workbench",
            default_branch="main",
        )
        db.add(Task(project=project, title="Write the runner"))
        db.commit()
        yield db
    app.dependency_overrides.clear()


@pytest.fixture
def client(session):
    return TestClient(app)


def _project(session) -> Project:
    return session.query(Project).one()


def test_adding_a_task_changes_the_fingerprint(session):
    """`COUNT` moves even though nothing about the existing row does."""
    project = _project(session)
    before = project_activity_fingerprint(session, project.id)

    session.add(Task(project=project, title="A second task"))
    session.commit()

    assert project_activity_fingerprint(session, project.id) != before


def test_deleting_a_task_changes_the_fingerprint(session):
    """The other direction of the same `COUNT` — nothing survives to bump
    its own `updated_at`, so only the count moving catches this."""
    project = _project(session)
    task = session.query(Task).one()
    before = project_activity_fingerprint(session, project.id)

    session.delete(task)
    session.commit()

    assert project_activity_fingerprint(session, project.id) != before


def test_toggling_a_task_status_changes_the_fingerprint(session):
    """`COUNT` alone would miss this — the row survives, only its
    `updated_at` moves."""
    project = _project(session)
    task = session.query(Task).one()
    before = project_activity_fingerprint(session, project.id)

    task.status = TaskStatus.DONE
    session.commit()

    assert project_activity_fingerprint(session, project.id) != before


def test_a_run_transitioning_status_changes_the_fingerprint(session):
    """A run has no `updated_at` of its own — this only works because every
    lifecycle transition is also a `run_events` row, which is what the
    fingerprint actually tracks (see `workbench.runs.lifecycle`/`runner`)."""
    project = _project(session)
    task = session.query(Task).one()
    run = Run(task_id=task.id, phase=RunPhase.EXECUTE, backend="claude", status=RunStatus.QUEUED)
    session.add(run)
    session.commit()

    session.add(
        RunEvent(
            run_id=run.id,
            seq=1,
            kind=RunEventKind.STATUS,
            payload={"status": "queued"},
        )
    )
    session.commit()
    queued = project_activity_fingerprint(session, project.id)

    run.status = RunStatus.RUNNING
    session.add(
        RunEvent(
            run_id=run.id,
            seq=2,
            kind=RunEventKind.STATUS,
            payload={"status": "running"},
        )
    )
    session.commit()
    running = project_activity_fingerprint(session, project.id)

    assert queued != running


def test_a_conversation_run_also_moves_the_fingerprint(session):
    """`Run.project_id` (a standing conversation, not scoped to any task) has
    to be tracked too — the "Continue conversation" banner reads it."""
    project = _project(session)
    before = project_activity_fingerprint(session, project.id)

    run = Run(
        project_id=project.id, phase=RunPhase.EXECUTE, backend="claude", status=RunStatus.RUNNING
    )
    session.add(run)
    session.commit()

    session.add(
        RunEvent(run_id=run.id, seq=1, kind=RunEventKind.STATUS, payload={"status": "running"})
    )
    session.commit()

    assert project_activity_fingerprint(session, project.id) != before


def test_an_unrelated_project_does_not_move_the_fingerprint(session):
    project = _project(session)
    before = project_activity_fingerprint(session, project.id)

    other = Project(
        user=project.user,
        owner="idm23",
        repo="something-else",
        github_url="https://github.com/idm23/something-else",
        default_branch="main",
    )
    session.add(other)
    session.commit()
    session.add(Task(project=other, title="Someone else's task"))
    session.commit()

    assert project_activity_fingerprint(session, project.id) == before


def test_the_route_serves_the_same_value_the_page_rendered_with(client, session):
    project = _project(session)

    page = client.get(f"/projects/{project.id}").text
    version = client.get(f"/projects/{project.id}/activity-version").json()["version"]

    assert f'data-activity-version="{version}"' in page


def test_the_route_reflects_a_change_after_the_page_was_rendered(client, session):
    project = _project(session)
    page = client.get(f"/projects/{project.id}").text
    initial = re.search(r'data-activity-version="([^"]*)"', page).group(1)

    task = session.query(Task).one()
    task.title = "Renamed"
    session.commit()

    version = client.get(f"/projects/{project.id}/activity-version").json()["version"]
    assert version != initial
