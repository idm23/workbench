"""The JSON API.

Driven in-process through FastAPI's TestClient rather than against a running
server, so these are ordinary unit tests: no port, no process, no waiting.

This is also the first route-level coverage in the project. The HTML routes are
still only exercised end to end by `scripts/smoke_test.py`, which needs a live
install — worth closing later, and the harness here is the thing that would do
it.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from workbench.app import app
from workbench.database.db import get_db, make_engine
from workbench.database.models import Base, Project, Run, RunOutcome, RunPhase, Task, User
from workbench.runs.store import create_run, mark_running

#: Module state rather than a second fixture: only a couple of tests need to
#: set up a `Run` directly (there is no HTTP route to create one), and a
#: second fixture just for them would be more machinery than the need
#: warrants. Set for the life of one `client` fixture at a time.
_engine: Engine | None = None


def _db() -> Session:
    """A session against the same engine the app under test is reading."""
    assert _engine is not None, "the client fixture must be active"
    return Session(_engine)


def _task(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    assert task is not None
    return task


def _run(db: Session, run_id: int) -> Run:
    run = db.get(Run, run_id)
    assert run is not None
    return run


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app, wired to a temporary database.

    `data/` is pointed at tmp too: the API reports whether a project is cloned,
    and that answer comes from the filesystem, so a stray clone in the real
    data directory would otherwise leak into these results.
    """
    global _engine

    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "test.db"))
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)

    def override():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override
    with Session(engine) as setup:
        setup.add(
            Project(
                user=User(name="ian"),
                owner="idm23",
                repo="workbench",
                github_url="https://github.com/idm23/workbench",
            )
        )
        setup.commit()

    _engine = engine
    with TestClient(app) as client:
        yield client

    _engine = None
    app.dependency_overrides.clear()


def test_projects_are_listed(client):
    response = client.get("/api/projects")

    assert response.status_code == 200
    assert [(p["owner"], p["repo"]) for p in response.json()] == [("idm23", "workbench")]


def test_a_project_reports_whether_it_is_cloned(client):
    """Derived from the filesystem, so a fresh instance says no."""
    assert client.get("/api/projects").json()[0]["cloned"] is False


def test_open_task_count_excludes_done(client):
    client.post("/api/projects/1/tasks", json={"title": "open one"})
    done = client.post("/api/projects/1/tasks", json={"title": "finished"}).json()
    client.patch(f"/api/tasks/{done['id']}", json={"status": "done"})

    assert client.get("/api/projects").json()[0]["open_tasks"] == 1


def test_a_task_can_be_created(client):
    response = client.post(
        "/api/projects/1/tasks", json={"title": "Ship the API", "body": "detail"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Ship the API"
    assert body["body"] == "detail"
    assert body["status"] == "open"
    assert body["children"] == []


def test_tasks_come_back_as_a_tree(client):
    parent = client.post("/api/projects/1/tasks", json={"title": "parent"}).json()
    client.post("/api/projects/1/tasks", json={"title": "child", "parent_id": parent["id"]})

    roots = client.get("/api/projects/1/tasks").json()

    assert len(roots) == 1
    assert roots[0]["title"] == "parent"
    assert [c["title"] for c in roots[0]["children"]] == ["child"]


def test_nesting_goes_deeper_than_one_level(client):
    parent = client.post("/api/projects/1/tasks", json={"title": "a"}).json()
    child = client.post(
        "/api/projects/1/tasks", json={"title": "b", "parent_id": parent["id"]}
    ).json()
    client.post("/api/projects/1/tasks", json={"title": "c", "parent_id": child["id"]})

    roots = client.get("/api/projects/1/tasks").json()

    assert roots[0]["children"][0]["children"][0]["title"] == "c"


def test_an_empty_title_is_rejected(client):
    assert client.post("/api/projects/1/tasks", json={"title": ""}).status_code == 422


def test_a_parent_in_another_project_is_rejected(client):
    """422 rather than 404: the request was coherent, just wrong."""
    response = client.post("/api/projects/1/tasks", json={"title": "x", "parent_id": 9999})

    assert response.status_code == 422
    assert "does not belong" in response.json()["detail"]


def test_status_can_be_patched(client):
    task = client.post("/api/projects/1/tasks", json={"title": "x"}).json()

    response = client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})

    assert response.status_code == 200
    assert response.json()["status"] == "done"


def test_a_patch_only_changes_what_it_names(client):
    task = client.post("/api/projects/1/tasks", json={"title": "keep", "body": "me"}).json()

    client.patch(f"/api/tasks/{task['id']}", json={"status": "blocked"})

    after = client.get("/api/projects/1/tasks").json()[0]
    assert (after["title"], after["body"], after["status"]) == ("keep", "me", "blocked")


def test_an_invalid_status_is_rejected(client):
    task = client.post("/api/projects/1/tasks", json={"title": "x"}).json()

    assert client.patch(f"/api/tasks/{task['id']}", json={"status": "nonsense"}).status_code == 422


def test_a_task_can_be_deleted_with_its_children(client):
    parent = client.post("/api/projects/1/tasks", json={"title": "parent"}).json()
    client.post("/api/projects/1/tasks", json={"title": "child", "parent_id": parent["id"]})

    assert client.delete(f"/api/tasks/{parent['id']}").status_code == 204
    assert client.get("/api/projects/1/tasks").json() == []


def test_unknown_ids_are_404(client):
    assert client.get("/api/projects/9999/tasks").status_code == 404
    assert client.post("/api/projects/9999/tasks", json={"title": "x"}).status_code == 404
    assert client.patch("/api/tasks/9999", json={"status": "done"}).status_code == 404
    assert client.delete("/api/tasks/9999").status_code == 404


def test_the_api_and_the_forms_agree(client):
    """Both write through the same store, so a task made either way reads back
    identically. This is the property that stops them drifting."""
    client.post("/api/projects/1/tasks", json={"title": "via the API"})
    client.post(
        "/projects/1/tasks",
        data={"title": "via the form", "body": "", "parent_id": ""},
        follow_redirects=True,
    )

    titles = [task["title"] for task in client.get("/api/projects/1/tasks").json()]
    assert titles == ["via the API", "via the form"]


# --- Subtasks: a plan's decomposition, or a live execute-run spin-off ------


def test_a_subtask_is_created_under_its_parent(client):
    parent = client.post("/api/projects/1/tasks", json={"title": "parent"}).json()

    response = client.post(f"/api/tasks/{parent['id']}/subtasks", json={"title": "child"})

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "child"
    assert body["parent_id"] == parent["id"]


def test_a_ready_subtask_sets_entry_phase_to_execute(client):
    parent = client.post("/api/projects/1/tasks", json={"title": "parent"}).json()

    client.post(
        f"/api/tasks/{parent['id']}/subtasks",
        json={"title": "child", "ready_to_execute": True},
    )

    with _db() as db:
        child = db.query(Task).filter_by(title="child").one()
        assert child.entry_phase is RunPhase.EXECUTE


def test_a_subtask_defaults_its_origin_to_the_parents_branch(client):
    parent = client.post("/api/projects/1/tasks", json={"title": "parent"}).json()
    with _db() as db:
        _task(db, parent["id"]).branch = "workbench/task-1-parent"
        db.commit()

    client.post(f"/api/tasks/{parent['id']}/subtasks", json={"title": "child"})

    with _db() as db:
        child = db.query(Task).filter_by(title="child").one()
        assert child.origin_ref == f"task:{parent['id']}"


def test_a_subtask_under_an_unbranched_parent_has_no_origin_set(client):
    parent = client.post("/api/projects/1/tasks", json={"title": "parent"}).json()

    client.post(f"/api/tasks/{parent['id']}/subtasks", json={"title": "child"})

    with _db() as db:
        child = db.query(Task).filter_by(title="child").one()
        assert child.origin_ref is None


def test_a_subtask_of_a_missing_task_is_404(client):
    assert client.post("/api/tasks/9999/subtasks", json={"title": "x"}).status_code == 404


# --- Outcomes: what the agent itself reports, live -------------------------


def _running_execute_run(client, task_id: int) -> int:
    """A run this test can report an outcome against.

    Set up directly rather than over HTTP: starting a real run needs an
    executor, which is out of scope here — these tests are about the
    outcome endpoint's own validation, not the run lifecycle.
    """
    with _db() as db:
        run = create_run(db, _task(db, task_id), RunPhase.EXECUTE, backend="fake")
        mark_running(db, run)
        return run.id


def test_an_outcome_is_recorded_on_the_run(client):
    task = client.post("/api/projects/1/tasks", json={"title": "x"}).json()
    run_id = _running_execute_run(client, task["id"])

    response = client.post(
        f"/api/runs/{run_id}/outcome",
        json={"outcome": "needs_replanning", "detail": "stuck"},
    )

    assert response.status_code == 204
    with _db() as db:
        run = _run(db, run_id)
        assert run.agent_outcome is RunOutcome.NEEDS_REPLANNING
        assert run.outcome_detail == "stuck"


def test_an_invalid_outcome_value_is_rejected(client):
    task = client.post("/api/projects/1/tasks", json={"title": "x"}).json()
    run_id = _running_execute_run(client, task["id"])

    response = client.post(f"/api/runs/{run_id}/outcome", json={"outcome": "nonsense"})

    assert response.status_code == 422


def test_an_outcome_for_a_non_running_run_is_rejected(client):
    task = client.post("/api/projects/1/tasks", json={"title": "x"}).json()
    with _db() as db:
        run = create_run(db, _task(db, task["id"]), RunPhase.EXECUTE, backend="fake")
        run_id = run.id

    response = client.post(f"/api/runs/{run_id}/outcome", json={"outcome": "finished"})

    assert response.status_code == 422
    assert "not running" in response.json()["detail"]


def test_an_outcome_for_a_plan_run_is_rejected(client):
    task = client.post("/api/projects/1/tasks", json={"title": "x"}).json()
    with _db() as db:
        run = create_run(db, _task(db, task["id"]), RunPhase.PLAN, backend="fake")
        mark_running(db, run)
        run_id = run.id

    response = client.post(f"/api/runs/{run_id}/outcome", json={"outcome": "finished"})

    assert response.status_code == 422
    assert "execute-phase" in response.json()["detail"]


def test_an_outcome_for_a_missing_run_is_404(client):
    response = client.post("/api/runs/9999/outcome", json={"outcome": "finished"})
    assert response.status_code == 404
