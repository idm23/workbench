"""Every POST a form submits.

The GET pages and the run-lifecycle POSTs are covered by `test_run_routes.py`,
`test_page_indicators.py`, and `test_setup_warnings.py`. What was still
uncovered — creating a user, adding a project, adding a task, changing a
task's status, deleting a task, and cloning a project's repository — was only
exercised end to end by `scripts/smoke_test.py`, which needs a live install.
That means a broken form was caught by a container run or a person on a
phone, not by this suite. This file closes that gap the same way
`test_run_routes.py` does: an in-process `TestClient` against a temporary
database, with `follow_redirects=False` so a redirect's target and query
string can be asserted directly.

Two of these routes have a real side effect that has to be faked rather than
exercised for real: `add_project` calls out to GitHub over HTTP, and
`clone_repository` shells out to real `git`. Both are patched by the name
imported into `app.py`, matching how `test_run_routes.py` patches
`local_checkout`/`sync_worktree`.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from workbench.app import app
from workbench.database.db import get_db, make_engine
from workbench.database.models import Base, Project, Task, TaskStatus, User
from workbench.git.github import RepoLookupUnavailable, RepoMetadata, RepoNotFound
from workbench.git.worktrees import Cloned, GitFailed


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A temporary database, seeded with one user, one project, and one task."""
    db_path = tmp_path / "data" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKBENCH_DB", str(db_path))
    engine = make_engine(f"sqlite+pysqlite:///{db_path}")
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
    return TestClient(app, follow_redirects=False)


def a_user(session) -> User:
    return session.query(User).one()


def a_project(session) -> Project:
    return session.query(Project).one()


def a_task(session, title="Write the runner") -> Task:
    return session.query(Task).filter_by(title=title).one()


# --- Creating a user ---------------------------------------------------------


def test_creating_a_user_redirects_home(client, session):
    response = client.post("/users", data={"name": "grace"})

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert session.query(User).filter_by(name="grace").one()


def test_a_users_name_is_trimmed(client, session):
    client.post("/users", data={"name": "  grace  "})

    assert session.query(User).filter_by(name="grace").one()


def test_a_blank_name_is_refused(client, session):
    response = client.post("/users", data={"name": "   "})

    assert "Enter+a+name" in response.headers["location"]
    assert session.query(User).filter_by(name="").count() == 0


def test_a_duplicate_name_is_refused(client, session):
    a_user(session)  # "ian", seeded by the fixture

    response = client.post("/users", data={"name": "ian"})

    assert "already+a+user+called+ian" in response.headers["location"]
    assert session.query(User).filter_by(name="ian").count() == 1


# --- Adding a project ---------------------------------------------------------


def test_adding_a_project_with_metadata(client, session, monkeypatch):
    monkeypatch.setattr(
        "workbench.app.fetch_repo_metadata",
        lambda ref: RepoMetadata(description="A tool.", default_branch="main"),
    )
    user = a_user(session)

    response = client.post(f"/users/{user.id}/projects", data={"reference": "octocat/hello"})

    assert response.status_code == 303
    assert response.headers["location"] == f"/users/{user.id}"
    project = session.query(Project).filter_by(repo="hello").one()
    assert project.owner == "octocat"
    assert project.description == "A tool."
    assert project.default_branch == "main"


def test_an_invalid_reference_is_refused_without_a_network_call(client, session):
    user = a_user(session)

    response = client.post(f"/users/{user.id}/projects", data={"reference": "not a repo"})

    assert "error=" in response.headers["location"]
    assert session.query(Project).filter_by(owner="not a repo").count() == 0


def test_a_repo_github_reports_missing_is_refused(client, session, monkeypatch):
    monkeypatch.setattr(
        "workbench.app.fetch_repo_metadata",
        lambda ref: RepoNotFound(slug="octocat/hello", message="octocat/hello was not found."),
    )
    user = a_user(session)

    response = client.post(f"/users/{user.id}/projects", data={"reference": "octocat/hello"})

    assert "was+not+found" in response.headers["location"]
    assert session.query(Project).filter_by(repo="hello").count() == 0


def test_an_unavailable_lookup_still_saves_the_project(client, session, monkeypatch):
    monkeypatch.setattr(
        "workbench.app.fetch_repo_metadata",
        lambda ref: RepoLookupUnavailable(message="GitHub is rate-limiting us."),
    )
    user = a_user(session)

    response = client.post(f"/users/{user.id}/projects", data={"reference": "octocat/hello"})

    project = session.query(Project).filter_by(repo="hello").one()
    assert project.description is None
    assert project.default_branch is None
    assert "Added+octocat" in response.headers["location"]
    assert "without+details" in response.headers["location"]


def test_a_duplicate_project_is_refused(client, session, monkeypatch):
    monkeypatch.setattr(
        "workbench.app.fetch_repo_metadata",
        lambda ref: RepoMetadata(description=None, default_branch=None),
    )
    user = a_user(session)

    response = client.post(f"/users/{user.id}/projects", data={"reference": "idm23/workbench"})

    assert "already+has" in response.headers["location"]
    assert session.query(Project).filter_by(repo="workbench").count() == 1


def test_adding_a_project_to_a_missing_user_is_a_404(client, session):
    response = client.post("/users/9999/projects", data={"reference": "octocat/hello"})

    assert response.status_code == 404


# --- Adding a task -------------------------------------------------------------


def test_adding_a_top_level_task(client, session):
    project = a_project(session)

    response = client.post(f"/projects/{project.id}/tasks", data={"title": "New task"})

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}"
    task = session.query(Task).filter_by(title="New task").one()
    assert task.parent_id is None


def test_adding_a_task_with_a_body(client, session):
    project = a_project(session)

    client.post(
        f"/projects/{project.id}/tasks",
        data={"title": "New task", "body": "Some detail."},
    )

    task = session.query(Task).filter_by(title="New task").one()
    assert task.body == "Some detail."


def test_a_blank_task_title_is_refused(client, session):
    project = a_project(session)

    response = client.post(f"/projects/{project.id}/tasks", data={"title": "   "})

    assert "Enter+a+title" in response.headers["location"]
    assert session.query(Task).filter_by(title="").count() == 0


def test_adding_a_subtask(client, session):
    project = a_project(session)
    parent = a_task(session)

    response = client.post(
        f"/projects/{project.id}/tasks",
        data={"title": "A subtask", "parent_id": str(parent.id)},
    )

    assert response.status_code == 303
    child = session.query(Task).filter_by(title="A subtask").one()
    assert child.parent_id == parent.id


def test_a_parent_from_another_project_is_refused(client, session):
    other = Project(
        user_id=a_user(session).id,
        owner="idm23",
        repo="other",
        github_url="https://github.com/idm23/other",
    )
    session.add(other)
    session.commit()
    outsider = a_task(session)

    response = client.post(
        f"/projects/{other.id}/tasks",
        data={"title": "Sneaky", "parent_id": str(outsider.id)},
    )

    assert "does+not+belong" in response.headers["location"]
    assert session.query(Task).filter_by(title="Sneaky").count() == 0


def test_adding_a_task_to_a_missing_project_is_a_404(client, session):
    response = client.post("/projects/9999/tasks", data={"title": "New task"})

    assert response.status_code == 404


# --- Changing a task's status --------------------------------------------------


def test_setting_a_valid_status(client, session):
    task = a_task(session)

    response = client.post(f"/tasks/{task.id}/status", data={"new_status": "done"})

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{task.project_id}"
    session.refresh(task)
    assert task.status is TaskStatus.DONE


def test_an_invalid_status_is_rejected_readably(client, session):
    task = a_task(session)

    response = client.post(f"/tasks/{task.id}/status", data={"new_status": "sideways"})

    assert "not+a+task+status" in response.headers["location"]
    session.refresh(task)
    assert task.status is TaskStatus.OPEN


def test_setting_the_status_of_a_missing_task_is_a_404(client, session):
    response = client.post("/tasks/9999/status", data={"new_status": "done"})

    assert response.status_code == 404


# --- Deleting a task ------------------------------------------------------------


def test_deleting_a_task(client, session):
    task = a_task(session)
    project_id = task.project_id

    response = client.post(f"/tasks/{task.id}/delete")

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/projects/{project_id}?notice=")
    assert "Deleted" in response.headers["location"]
    assert "Write+the+runner" in response.headers["location"]
    assert session.query(Task).filter_by(id=task.id).count() == 0


def test_deleting_a_parent_removes_its_children(client, session):
    parent = a_task(session)
    child = Task(project_id=parent.project_id, parent_id=parent.id, title="A child")
    session.add(child)
    session.commit()
    child_id = child.id

    client.post(f"/tasks/{parent.id}/delete")

    assert session.query(Task).filter_by(id=parent.id).count() == 0
    assert session.query(Task).filter_by(id=child_id).count() == 0


def test_deleting_a_task_with_a_worktree_removes_it(client, session, monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    calls: list[tuple] = []
    monkeypatch.setattr("workbench.tasks.store.local_checkout", lambda *_: checkout)
    monkeypatch.setattr(
        "workbench.tasks.store.remove_worktree",
        lambda repo, path: calls.append((repo, path)),
    )
    task = a_task(session)
    task.worktree_path = "/somewhere/worktree"
    session.commit()

    client.post(f"/tasks/{task.id}/delete")

    assert len(calls) == 1
    assert calls[0][0] == checkout
    assert calls[0][1] == Path("/somewhere/worktree")


def test_deleting_a_task_with_no_worktree_does_not_touch_one(
    client, session, monkeypatch, tmp_path
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    calls: list[tuple] = []
    monkeypatch.setattr("workbench.tasks.store.local_checkout", lambda *_: checkout)
    monkeypatch.setattr(
        "workbench.tasks.store.remove_worktree",
        lambda repo, path: calls.append((repo, path)),
    )
    task = a_task(session)  # no worktree_path set

    client.post(f"/tasks/{task.id}/delete")

    assert calls == []


def test_deleting_a_missing_task_is_a_404(client, session):
    response = client.post("/tasks/9999/delete")

    assert response.status_code == 404


# --- Cloning a project ----------------------------------------------------------


def test_cloning_a_project(client, session, monkeypatch, tmp_path):
    cloned_to = tmp_path / "clone"
    monkeypatch.setattr("workbench.app.clone_project", lambda *a, **k: Cloned(cloned_to))
    project = a_project(session)

    response = client.post(f"/projects/{project.id}/clone")

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/projects/{project.id}?notice=")
    assert "Cloned+to" in response.headers["location"]


def test_a_failed_clone_shows_the_reason(client, session, monkeypatch):
    monkeypatch.setattr(
        "workbench.app.clone_project",
        lambda *a, **k: GitFailed(message="Clone failed.", stderr="repository not found"),
    )
    project = a_project(session)

    response = client.post(f"/projects/{project.id}/clone")

    assert "Clone+failed" in response.headers["location"]
    assert "repository+not+found" in response.headers["location"]


def test_cloning_a_missing_project_is_a_404(client, session):
    response = client.post("/projects/9999/clone")

    assert response.status_code == 404
