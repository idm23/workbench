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
from workbench.database.db import get_db, get_engine, get_session_factory, make_engine
from workbench.database.models import Base, Project, Run, RunStatus, Task, User
from workbench.runs import lifecycle
from workbench.runs import stream as stream_module
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
    """The app's database, and the process-wide engine, pointed at one file.

    Both, and at the *same* path, which matters here in a way it does not for
    the other route tests. The event stream cannot use the request's session —
    it outlives the request that opened it — so it reaches the database through
    `session_scope`, which builds its own engine from `WORKBENCH_DB`. Point
    those two at different files and the page renders from one while the stream
    reads from the other.
    """
    db_path = tmp_path / "data" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKBENCH_DB", str(db_path))
    monkeypatch.setenv("WORKBENCH_MAX_CONCURRENT_RUNS", "2")
    get_engine.cache_clear()
    get_session_factory.cache_clear()
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
        db.add(Task(project=project, title="Something else"))
        db.commit()
        yield db
    app.dependency_overrides.clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture(autouse=True)
def brisk(monkeypatch):
    """Real streaming, minus the waiting.

    A finished run's stream sweeps a few more times before closing, to be
    correct whatever order the writer committed in. At the real interval that
    is three seconds per test, which is three seconds CI spends watching a
    clock rather than checking anything.
    """
    monkeypatch.setattr(stream_module, "POLL_SECONDS", 0.001)


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


# --- Reading a run back ----------------------------------------------------


def a_finished_run(session, *, status=RunStatus.SUCCEEDED):
    from workbench.database.models import RunEventKind, RunPhase
    from workbench.runs.store import append_event, create_run, finish_run, mark_running

    run = create_run(session, a_task(session), RunPhase.EXECUTE, backend="claude")
    mark_running(session, run)
    append_event(session, run.id, RunEventKind.TEXT, {"text": "Looking at the code."})
    append_event(session, run.id, RunEventKind.TOOL_USE, {"name": "Bash"})
    finish_run(session, run, status, summary="Added the tests.", diffstat=" a.py | 2 +-")
    return run


def test_a_run_page_renders_what_happened(client, session):
    run = a_finished_run(session)

    page = client.get(f"/runs/{run.id}").text

    assert "Looking at the code." in page
    assert "Added the tests." in page
    assert "a.py" in page


def test_the_page_renders_without_the_stream(client, session):
    """A run read back a week later has no stream to open."""
    run = a_finished_run(session)

    page = client.get(f"/runs/{run.id}").text

    assert "EventSource" not in page


def test_a_live_run_page_opens_a_stream_from_where_it_got_to(client, session, executor):
    client.post(f"/tasks/{a_task(session).id}/runs", data={"phase": "plan"})
    run = session.query(Run).one()

    page = client.get(f"/runs/{run.id}").text

    assert "EventSource" in page
    assert f"/runs/{run.id}/events?after=" in page


def test_the_reported_cost_is_labelled_as_not_a_bill(client, session):
    """Under a subscription it is a token valuation, not money charged."""
    run = a_finished_run(session)
    run.total_cost_usd = 0.42
    session.commit()

    assert "not an amount charged" in client.get(f"/runs/{run.id}").text


def test_a_missing_run_is_a_404(client, session):
    assert client.get("/runs/999").status_code == 404


def test_the_stream_is_served_as_server_sent_events(client, session):
    run = a_finished_run(session)

    response = client.get(f"/runs/{run.id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    # Without this a buffering proxy waits for the whole response, which is
    # the one thing streaming cannot survive.
    assert response.headers["x-accel-buffering"] == "no"


def test_the_stream_replays_the_whole_log(client, session):
    run = a_finished_run(session)

    body = client.get(f"/runs/{run.id}/events").text

    assert "Looking at the code." in body
    assert body.count("\nevent: ") >= 3
    assert "event: end" in body


def test_the_stream_resumes_from_the_last_event_id_header(client, session):
    """What a browser sends by itself on reconnect."""
    run = a_finished_run(session)

    body = client.get(f"/runs/{run.id}/events", headers={"Last-Event-ID": "2"}).text

    assert "Looking at the code." not in body
    assert "id: 3" in body


def test_the_after_parameter_resumes_too(client, session):
    """What the page uses on first load, having already rendered the past."""
    run = a_finished_run(session)

    body = client.get(f"/runs/{run.id}/events?after=2").text

    assert "Looking at the code." not in body
