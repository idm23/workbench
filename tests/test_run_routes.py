"""Starting and stopping a run from the page.

The refusals matter more than the happy path here. Every one of them is an
ordinary answer to a button press — the machine is busy, that task is already
being worked, the unit would not start — and each has to come back as something
readable on a phone rather than as a 500 or a silent redirect.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from workbench.app import app
from workbench.database.db import get_db, make_engine
from workbench.database.models import Base, Project, Run, RunStatus, Task, User
from workbench.runs import lifecycle
from workbench.runs.executors import Started, StartRefused


class FakeExecutor:
    """Starts nothing, records everything."""

    name = "fake"

    def __init__(self, refuses: str | None = None, alive: bool = True) -> None:
        self.refuses = refuses
        self.alive = alive
        self.started: list[int] = []
        self.cancelled: list[str] = []

    def start(self, run_id: int):
        if self.refuses:
            return StartRefused(self.refuses)
        self.started.append(run_id)
        return Started(f"fake-{run_id}")

    def cancel(self, handle: str) -> bool:
        self.cancelled.append(handle)
        return self.alive

    def is_running(self, handle: str) -> bool:
        return self.alive


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "test.db"))
    monkeypatch.setenv("WORKBENCH_MAX_CONCURRENT_RUNS", "2")
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
        db.add(Task(project=project, title="Something else"))
        db.commit()
        yield db
    app.dependency_overrides.clear()


@pytest.fixture
def client(session):
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def executor(monkeypatch):
    fake = FakeExecutor()
    monkeypatch.setattr(lifecycle, "get_executor", lambda _name=None: fake)
    return fake


@pytest.fixture
def cloned(session, monkeypatch, tmp_path):
    """Pretend the project is checked out, so the page offers the button."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr("workbench.app.local_checkout", lambda *_: checkout)
    return checkout


def a_task(session, title="Write the runner") -> Task:
    return session.query(Task).filter_by(title=title).one()


def test_starting_a_run_redirects_with_a_notice(client, session, executor):
    task = a_task(session)

    response = client.post(f"/tasks/{task.id}/runs", data={"phase": "plan"})

    assert response.status_code == 303
    assert "Run+1+started" in response.headers["location"]
    assert executor.started == [1]


def test_the_run_is_recorded_against_the_task(client, session, executor):
    task = a_task(session)

    client.post(f"/tasks/{task.id}/runs", data={"phase": "plan"})

    run = session.query(Run).one()
    assert run.task_id == task.id
    assert run.executor == "fake"
    assert run.status is RunStatus.QUEUED


def test_a_second_run_on_the_same_task_is_refused(client, session, executor):
    """One task, one worktree."""
    task = a_task(session)
    client.post(f"/tasks/{task.id}/runs", data={"phase": "plan"})

    response = client.post(f"/tasks/{task.id}/runs", data={"phase": "plan"})

    assert "already+working" in response.headers["location"]


def test_the_concurrency_cap_is_reported_not_enforced_silently(
    client, session, executor, monkeypatch
):
    monkeypatch.setenv("WORKBENCH_MAX_CONCURRENT_RUNS", "1")
    client.post(f"/tasks/{a_task(session).id}/runs", data={"phase": "plan"})

    response = client.post(f"/tasks/{a_task(session, 'Something else').id}/runs")

    assert "error=" in response.headers["location"]
    assert "limit+is+1" in response.headers["location"]


def test_a_parent_task_cannot_be_run(client, session, executor):
    """It describes work rather than being work."""
    parent = a_task(session)
    child = Task(project_id=parent.project_id, parent_id=parent.id, title="A child")
    session.add(child)
    session.commit()

    response = client.post(f"/tasks/{parent.id}/runs", data={"phase": "plan"})

    assert "sub-task" in response.headers["location"]
    assert executor.started == []


def test_an_executor_that_refuses_leaves_a_readable_reason(client, session, monkeypatch):
    monkeypatch.setattr(
        lifecycle, "get_executor", lambda _name=None: FakeExecutor(refuses="Access denied")
    )

    response = client.post(f"/tasks/{a_task(session).id}/runs", data={"phase": "plan"})

    assert "Access+denied" in response.headers["location"]
    assert session.query(Run).one().status is RunStatus.FAILED


def test_an_unknown_phase_is_rejected_readably(client, session, executor):
    """The form never sends this, so it is a hand-made request."""
    response = client.post(f"/tasks/{a_task(session).id}/runs", data={"phase": "sideways"})

    assert "not+a+run+phase" in response.headers["location"]


def test_cancelling_asks_the_executor_to_stop(client, session, executor):
    client.post(f"/tasks/{a_task(session).id}/runs", data={"phase": "plan"})

    response = client.post("/runs/1/cancel")

    assert response.status_code == 303
    assert executor.cancelled == ["fake-1"]


def test_cancelling_a_finished_run_says_so(client, session, executor):
    client.post(f"/tasks/{a_task(session).id}/runs", data={"phase": "plan"})
    run = session.query(Run).one()
    run.status = RunStatus.SUCCEEDED
    session.commit()

    response = client.post(f"/runs/{run.id}/cancel")

    assert "already+succeeded" in response.headers["location"]


def test_cancelling_a_run_that_does_not_exist_is_a_404(client, session):
    assert client.post("/runs/999/cancel").status_code == 404


# --- What the page offers --------------------------------------------------


def test_a_runnable_task_offers_to_plan(client, session, cloned):
    page = client.get(f"/projects/{a_task(session).project_id}").text

    assert "/runs" in page
    assert ">Plan<" in page


def test_nothing_is_offered_before_the_project_is_cloned(client, session):
    """There is nowhere for an agent to work yet."""
    page = client.get(f"/projects/{a_task(session).project_id}").text

    assert ">Plan<" not in page


def test_a_parent_task_is_not_offered_a_run(client, session, cloned):
    parent = a_task(session)
    session.add(Task(project_id=parent.project_id, parent_id=parent.id, title="A child"))
    session.commit()

    page = client.get(f"/projects/{parent.project_id}").text

    assert page.count(">Plan<") == 2


def test_a_running_task_offers_to_stop_instead(client, session, cloned, executor):
    task = a_task(session)
    client.post(f"/tasks/{task.id}/runs", data={"phase": "plan"})

    page = client.get(f"/projects/{task.project_id}").text

    assert "/runs/1/cancel" in page
    assert "Stop run" in page
