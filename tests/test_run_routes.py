"""Starting and stopping a run from the page.

The refusals matter more than the happy path here. Every one of them is an
ordinary answer to a button press — the machine is busy, that task is already
being worked, the unit would not start — and each has to come back as something
readable on a phone rather than as a 500 or a silent redirect.
"""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from workbench.app import app
from workbench.database.db import get_db, get_engine, get_session_factory, make_engine
from workbench.database.models import (
    Base,
    Project,
    Run,
    RunPhase,
    RunStatus,
    Task,
    TaskStatus,
    User,
)
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


# --- Choosing where to branch from ------------------------------------------


def test_the_default_origin_is_recorded_as_prod(client, session, executor):
    task = a_task(session)

    client.post(f"/tasks/{task.id}/runs", data={"phase": "plan"})

    session.refresh(task)
    assert task.origin_ref is None


def test_choosing_staging_is_recorded_on_the_task(client, session, executor):
    task = a_task(session)

    client.post(f"/tasks/{task.id}/runs", data={"phase": "plan", "origin": "staging"})

    session.refresh(task)
    assert task.origin_ref == "staging"


def test_an_invalid_origin_is_rejected_before_a_run_is_queued(client, session, executor):
    task = a_task(session)

    response = client.post(
        f"/tasks/{task.id}/runs", data={"phase": "plan", "origin": "not-a-real-choice"}
    )

    assert "not+a+valid+origin" in response.headers["location"]
    assert session.query(Run).count() == 0


def test_a_branched_sibling_is_a_valid_origin(client, session, executor):
    parent = a_task(session)
    sibling = Task(project_id=parent.project_id, parent_id=parent.id, title="sibling")
    task = Task(project_id=parent.project_id, parent_id=parent.id, title="task", branch=None)
    session.add_all([sibling, task])
    session.commit()
    sibling.branch = "workbench/task-3-sibling"
    session.commit()

    response = client.post(
        f"/tasks/{task.id}/runs", data={"phase": "plan", "origin": f"task:{sibling.id}"}
    )

    assert response.status_code == 303
    session.refresh(task)
    assert task.origin_ref == f"task:{sibling.id}"


def test_the_origin_is_not_re_asked_once_a_worktree_exists(client, session, executor):
    """Its branch is already fixed, so a later run has nothing to choose."""
    task = a_task(session)
    task.worktree_path = "/somewhere"
    task.branch = "workbench/task-1-write-the-runner"
    task.origin_ref = "staging"
    session.commit()

    response = client.post(
        f"/tasks/{task.id}/runs", data={"phase": "plan", "origin": "not-a-real-choice"}
    )

    assert response.status_code == 303
    session.refresh(task)
    assert task.origin_ref == "staging"


# --- Retrying a failed run ---------------------------------------------------


def test_retrying_starts_a_new_run_of_the_same_phase(client, session, executor):
    """Not "plan" by default — the Retry button posts the failed run's own
    phase, so a failed execute run resumes execute rather than replanning."""
    task = a_task(session)
    task.worktree_path = "/somewhere"
    session.commit()
    session.add(
        Run(task_id=task.id, phase=RunPhase.EXECUTE, backend="fake", status=RunStatus.FAILED)
    )
    session.commit()

    response = client.post(f"/tasks/{task.id}/runs", data={"phase": "execute"})

    assert response.status_code == 303
    new_run = session.query(Run).filter_by(status=RunStatus.QUEUED).one()
    assert new_run.phase is RunPhase.EXECUTE
    assert executor.started == [new_run.id]


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


# --- Approving a plan --------------------------------------------------------


def _plan_awaiting_review(session, task=None, proposed_subtasks=None):
    from workbench.database.models import RunPhase
    from workbench.runs.store import create_run, finish_run

    run = create_run(session, task or a_task(session), RunPhase.PLAN, backend="claude")
    finish_run(
        session,
        run,
        RunStatus.AWAITING_REVIEW,
        plan="Here is the plan.",
        proposed_subtasks=proposed_subtasks,
    )
    return run


def test_approving_a_plan_with_no_subtasks_starts_execute(client, session, executor):
    from workbench.database.models import RunPhase

    run = _plan_awaiting_review(session)

    response = client.post(f"/runs/{run.id}/approve")

    assert response.status_code == 303
    assert "execute" in response.headers["location"]
    assert len(executor.started) == 1
    new_run = session.get(Run, executor.started[0])
    assert new_run.phase is RunPhase.EXECUTE
    assert new_run.task_id == run.task_id


def test_approving_a_decomposed_plan_creates_its_subtasks_instead(client, session, executor):
    task = a_task(session)
    run = _plan_awaiting_review(
        session,
        task=task,
        proposed_subtasks={
            "subtasks": [
                {"title": "Do part A", "body": "details", "ready_to_execute": True},
                {"title": "Investigate B", "body": None, "ready_to_execute": False},
            ]
        },
    )

    response = client.post(f"/runs/{run.id}/approve")

    assert response.status_code == 303
    assert "Created+2+subtask" in response.headers["location"]
    assert executor.started == []  # nothing was run — only tasks were created
    children = session.query(Task).filter_by(parent_id=task.id).order_by(Task.id).all()
    assert [c.title for c in children] == ["Do part A", "Investigate B"]
    from workbench.database.models import RunPhase

    assert children[0].entry_phase is RunPhase.EXECUTE
    assert children[1].entry_phase is None


def test_a_decomposed_subtask_defaults_its_origin_to_the_parent(client, session, executor):
    task = a_task(session)
    task.branch = "workbench/task-1-write-the-runner"
    session.commit()
    run = _plan_awaiting_review(
        session, task=task, proposed_subtasks={"subtasks": [{"title": "x", "body": None}]}
    )

    client.post(f"/runs/{run.id}/approve")

    child = session.query(Task).filter_by(title="x").one()
    assert child.origin_ref == f"task:{task.id}"


def test_approving_a_run_that_is_not_awaiting_review_is_refused(client, session, executor):
    run = _plan_awaiting_review(session)
    run.status = RunStatus.SUCCEEDED
    session.commit()

    response = client.post(f"/runs/{run.id}/approve")

    assert "no+plan+awaiting+approval" in response.headers["location"]


def test_approving_a_non_plan_run_is_refused(client, session, executor):
    from workbench.database.models import RunPhase
    from workbench.runs.store import create_run, finish_run

    run = create_run(session, a_task(session), RunPhase.EXECUTE, backend="claude")
    finish_run(session, run, RunStatus.AWAITING_REVIEW)

    response = client.post(f"/runs/{run.id}/approve")

    assert "no+plan+awaiting+approval" in response.headers["location"]


def test_approving_a_missing_run_is_a_404(client, session):
    assert client.post("/runs/999/approve").status_code == 404


# --- Syncing a worktree with its origin -------------------------------------


def test_syncing_a_task_with_no_worktree_is_refused(client, session):
    response = client.post(f"/tasks/{a_task(session).id}/sync")

    assert "no+worktree" in response.headers["location"]


def test_syncing_while_a_run_is_active_is_refused(client, session, executor):
    task = a_task(session)
    task.worktree_path = "/somewhere"
    session.commit()
    client.post(f"/tasks/{task.id}/runs", data={"phase": "plan"})

    response = client.post(f"/tasks/{task.id}/sync")

    assert "in+progress" in response.headers["location"]


def test_syncing_an_uncloned_project_is_refused(client, session):
    task = a_task(session)
    task.worktree_path = "/somewhere"
    session.commit()

    response = client.post(f"/tasks/{task.id}/sync")

    assert "not+cloned" in response.headers["location"]


def test_a_successful_sync_says_so(client, session, monkeypatch, cloned):
    from workbench.git.worktrees import Synced

    task = a_task(session)
    task.worktree_path = "/somewhere"
    session.commit()
    monkeypatch.setattr(
        "workbench.app.sync_worktree", lambda *a, **k: Synced("Already up to date.")
    )

    response = client.post(f"/tasks/{task.id}/sync")

    assert "Synced+with+main" in response.headers["location"]


def test_a_refused_sync_shows_the_reason(client, session, monkeypatch, cloned):
    from workbench.git.worktrees import SyncRefused

    task = a_task(session)
    task.worktree_path = "/somewhere"
    session.commit()
    monkeypatch.setattr(
        "workbench.app.sync_worktree",
        lambda *a, **k: SyncRefused("has commits of its own"),
    )

    response = client.post(f"/tasks/{task.id}/sync")

    assert "commits+of+its+own" in response.headers["location"]


def test_syncing_a_missing_task_is_a_404(client, session):
    assert client.post("/tasks/9999/sync").status_code == 404


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


def test_a_parent_task_offers_to_collapse_its_subtasks(client, session, cloned):
    parent = a_task(session)
    session.add(Task(project_id=parent.project_id, parent_id=parent.id, title="A child"))
    session.commit()

    page = client.get(f"/projects/{parent.project_id}").text

    assert 'class="twist"' in page
    assert 'data-depth="0"' in page
    assert 'data-depth="1"' in page


def test_a_leaf_task_offers_no_collapse_toggle(client, session, cloned):
    """Nothing to collapse — there is no subtree under a leaf."""
    page = client.get(f"/projects/{a_task(session).project_id}").text

    assert 'class="twist"' not in page


def test_a_parent_task_shows_a_progress_meter(client, session, cloned):
    parent = a_task(session)
    child = Task(project_id=parent.project_id, parent_id=parent.id, title="A child")
    session.add(child)
    session.commit()
    child.status = TaskStatus.DONE
    session.commit()

    page = client.get(f"/projects/{parent.project_id}").text

    assert "1/1 done" in page
    assert 'aria-valuenow="100"' in page


def test_a_short_task_body_is_shown_in_full(client, session, cloned):
    task = a_task(session)
    task.body = "A short note."
    session.commit()

    page = client.get(f"/projects/{task.project_id}").text

    assert "A short note." in page
    assert "<details" not in page


def test_a_long_task_body_is_folded_behind_a_toggle(client, session, cloned):
    """The "more"/"less" affordance itself is CSS `content`, invisible to a
    server-rendered response — what the page actually has to emit is the
    `<details>`/`<summary>` pair a browser turns into that toggle."""
    task = a_task(session)
    task.body = "word " * 100
    session.commit()

    page = client.get(f"/projects/{task.project_id}").text

    assert '<details class="task-note">' in page
    assert "<summary>" in page


def test_a_runnable_task_offers_an_origin_picker(client, session, cloned):
    page = client.get(f"/projects/{a_task(session).project_id}").text

    assert 'name="origin"' in page
    assert "main (prod)" in page
    assert "staging (dev)" in page


def test_a_task_with_an_existing_worktree_is_not_offered_a_picker(client, session, cloned):
    """Its branch is already fixed, so there is nothing left to choose.

    The project's other seeded task has no worktree, so the picker still
    appears once on the page — just not for this one.
    """
    task = a_task(session)
    task.worktree_path = "/somewhere"
    task.branch = "workbench/task-1-write-the-runner"
    session.commit()

    page = client.get(f"/projects/{task.project_id}").text

    assert page.count('name="origin"') == 1


def test_a_running_task_offers_to_stop_instead(client, session, cloned, executor):
    task = a_task(session)
    client.post(f"/tasks/{task.id}/runs", data={"phase": "plan"})

    page = client.get(f"/projects/{task.project_id}").text

    assert "/runs/1/cancel" in page
    assert "Stop run" in page


def test_a_task_with_a_worktree_offers_to_sync(client, session, cloned):
    task = a_task(session)
    task.worktree_path = "/somewhere"
    task.branch = "workbench/task-1-write-the-runner"
    session.commit()

    page = client.get(f"/projects/{task.project_id}").text

    assert f"/tasks/{task.id}/sync" in page
    assert ">Sync<" in page


def test_a_task_with_no_worktree_yet_is_not_offered_a_sync(client, session, cloned):
    """Its other seeded task has one, so the page should still show one Sync."""
    task = a_task(session)
    task.worktree_path = "/somewhere"
    session.commit()

    page = client.get(f"/projects/{task.project_id}").text

    assert page.count(">Sync<") == 1


def test_a_task_actively_running_is_not_offered_a_sync(client, session, cloned, executor):
    """Nothing should touch a worktree a process is writing to right now."""
    task = a_task(session)
    task.worktree_path = "/somewhere"
    session.commit()
    client.post(f"/tasks/{task.id}/runs", data={"phase": "plan"})

    page = client.get(f"/projects/{task.project_id}").text

    assert ">Sync<" not in page


def test_a_plan_awaiting_review_still_offers_to_sync(client, session, cloned):
    """The exact situation this exists for: nothing is actively running."""
    task = a_task(session)
    task.worktree_path = "/somewhere"
    session.commit()
    _plan_awaiting_review(session, task=task)

    page = client.get(f"/projects/{task.project_id}").text

    assert ">Sync<" in page


def test_a_plan_awaiting_review_offers_to_approve(client, session, cloned):
    task = a_task(session)
    run = _plan_awaiting_review(session, task=task)

    page = client.get(f"/projects/{task.project_id}").text

    assert f"/runs/{run.id}/approve" in page
    assert ">Execute<" in page
    assert "Discard" in page


def test_a_decomposing_plan_says_how_many_subtasks(client, session, cloned):
    task = a_task(session)
    run = _plan_awaiting_review(
        session, task=task, proposed_subtasks={"subtasks": [{"title": "a"}, {"title": "b"}]}
    )

    page = client.get(f"/projects/{task.project_id}").text

    assert f"/runs/{run.id}/approve" in page
    assert "Approve (2 subtasks)" in page


def test_a_needs_replanning_execute_run_offers_to_plan_again(client, session, cloned):
    from workbench.database.models import RunOutcome, RunPhase, TaskStatus
    from workbench.runs.store import create_run, finish_run

    task = a_task(session)
    run = create_run(session, task, RunPhase.EXECUTE, backend="claude")
    run.agent_outcome = RunOutcome.NEEDS_REPLANNING
    task.status = TaskStatus.BLOCKED
    session.commit()
    finish_run(session, run, RunStatus.AWAITING_REVIEW, summary="stuck")

    page = client.get(f"/projects/{task.project_id}").text

    assert f"/tasks/{task.id}/runs" in page
    assert ">Plan<" in page
    # Re-planning resumes the same task, not a fresh origin choice.
    assert f"/runs/{run.id}/approve" not in page


def test_a_done_task_is_not_offered_another_run(client, session, cloned):
    """The project's other seeded task is still open, so a Plan button
    remains on the page — just not on this one."""
    task = a_task(session)
    task.status = TaskStatus.DONE
    session.commit()

    page = client.get(f"/projects/{task.project_id}").text

    assert page.count(">Plan<") == 1
    assert ">Execute<" not in page


def test_an_execute_ready_task_offers_to_execute_first(client, session, cloned):
    from workbench.database.models import RunPhase

    task = a_task(session)
    task.entry_phase = RunPhase.EXECUTE
    session.commit()

    page = client.get(f"/projects/{task.project_id}").text

    assert page.count(">Execute<") == 1
    assert page.count(">Plan<") == 1  # the project's other, unplanned task


# --- Typing into a run while it goes ----------------------------------------


def _running_run(session) -> Run:
    from workbench.database.models import RunPhase
    from workbench.runs.store import create_run, mark_running

    run = create_run(session, a_task(session), RunPhase.EXECUTE, backend="claude")
    mark_running(session, run)
    return run


def test_sending_a_message_queues_it_and_logs_it(client, session):
    from workbench.database.models import RunEvent, RunEventKind, RunInput

    run = _running_run(session)

    response = client.post(f"/runs/{run.id}/message", data={"body": "also check the tests"})

    assert response.status_code == 303
    queued = session.query(RunInput).filter_by(run_id=run.id).one()
    assert queued.body == "also check the tests"
    logged = session.query(RunEvent).filter_by(run_id=run.id, kind=RunEventKind.INPUT).one()
    assert logged.payload == {"text": "also check the tests"}


def test_sending_an_empty_message_is_refused(client, session):
    run = _running_run(session)

    response = client.post(f"/runs/{run.id}/message", data={"body": "   "})

    assert "Type+something" in response.headers["location"]


def test_sending_a_message_to_a_run_that_is_not_running_is_refused(client, session):
    run = a_finished_run(session)

    response = client.post(f"/runs/{run.id}/message", data={"body": "hello"})

    assert "not+active" in response.headers["location"]


def test_sending_a_message_to_a_missing_run_is_404(client, session):
    assert client.post("/runs/999/message", data={"body": "hello"}).status_code == 404


def test_a_running_run_offers_to_send_a_message(client, session):
    run = _running_run(session)

    page = client.get(f"/runs/{run.id}").text

    assert f'action="/runs/{run.id}/message"' in page
    assert ">Send<" in page


def test_a_finished_run_does_not_offer_to_send_a_message(client, session):
    run = a_finished_run(session)

    page = client.get(f"/runs/{run.id}").text

    assert "/message" not in page


def _queued_run(session) -> Run:
    """A run that exists but whose runner has not started yet.

    Not a contrived state: it is what every conversation looks like at the
    moment the page is first rendered.
    """
    from workbench.database.models import RunPhase
    from workbench.runs.store import create_run

    return create_run(session, a_task(session), RunPhase.EXECUTE, backend="claude")


def _reply_form(page: str) -> str:
    """The reply form's opening tag, or "" when the page has none."""
    match = re.search(r'<form[^>]*id="reply"[^>]*>', page)
    return match.group(0) if match else ""


def test_a_queued_run_renders_the_message_box_hidden_rather_than_not_at_all(client, session):
    """The bug this fixes, and the reason it was invisible.

    Starting a conversation redirects here the instant the row exists, while
    the run is still queued and the runner is a process systemd has only just
    been asked to start. A form that is only *rendered* once the run is
    running is therefore a form nobody ever sees: the stream appends events
    and never re-renders the page, so the box arrived only for whoever
    happened to reload mid-run.
    """
    run = _queued_run(session)

    page = client.get(f"/runs/{run.id}").text

    assert f'action="/runs/{run.id}/message"' in page
    assert "hidden" in _reply_form(page)


def test_a_running_run_shows_the_message_box_straight_away(client, session):
    """Rendered hidden only while there is nothing listening yet."""
    run = _running_run(session)

    form = _reply_form(client.get(f"/runs/{run.id}").text)

    assert form, "the run page rendered no reply form at all"
    assert "hidden" not in form


def test_the_page_reveals_the_message_box_from_the_stream(client, session):
    """The other half: without this listener the form stays hidden forever,
    because the only reload this page does is the one when the run ends."""
    run = _queued_run(session)

    page = client.get(f"/runs/{run.id}").text

    assert 'source.addEventListener("status"' in page
    assert "reply.hidden" in page


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


# --- Talking directly to a project ------------------------------------------


def a_project(session) -> Project:
    return session.query(Project).one()


def test_talking_to_a_project_starts_a_conversation(client, session, executor):
    project = a_project(session)

    response = client.post(f"/projects/{project.id}/conversation")

    assert response.status_code == 303
    run = session.query(Run).one()
    assert response.headers["location"] == f"/runs/{run.id}"
    assert run.project_id == project.id
    assert run.task_id is None
    assert run.phase is RunPhase.CONVERSATION
    assert executor.started == [run.id]


def test_a_second_click_resumes_the_first_conversation_rather_than_duplicating(
    client, session, executor
):
    project = a_project(session)
    client.post(f"/projects/{project.id}/conversation")

    response = client.post(f"/projects/{project.id}/conversation")

    assert response.status_code == 303
    assert response.headers["location"] == "/runs/1"
    assert session.query(Run).count() == 1


def test_a_conversation_and_a_task_run_share_the_concurrency_cap(
    client, session, executor, monkeypatch
):
    monkeypatch.setenv("WORKBENCH_MAX_CONCURRENT_RUNS", "1")
    project = a_project(session)
    client.post(f"/tasks/{a_task(session).id}/runs", data={"phase": "plan"})

    response = client.post(f"/projects/{project.id}/conversation")

    assert "error=" in response.headers["location"]
    assert "limit+is+1" in response.headers["location"]


def test_talking_to_a_missing_project_is_a_404(client, session):
    assert client.post("/projects/9999/conversation").status_code == 404


def test_nothing_offers_to_talk_before_the_project_is_cloned(client, session):
    """There is nowhere for the conversation to run yet."""
    project = a_project(session)

    page = client.get(f"/projects/{project.id}").text

    assert "Talk to this project" not in page


def test_a_cloned_project_offers_to_talk(client, session, cloned):
    project = a_project(session)

    page = client.get(f"/projects/{project.id}").text

    assert f'action="/projects/{project.id}/conversation"' in page
    assert "Talk to this project" in page


def test_a_project_page_offers_to_continue_an_active_conversation(
    client, session, cloned, executor
):
    project = a_project(session)
    client.post(f"/projects/{project.id}/conversation")

    page = client.get(f"/projects/{project.id}").text

    assert "Continue conversation" in page
    assert "/runs/1" in page
    assert "Talk to this project" not in page


def test_a_project_scoped_run_page_renders_without_a_task(client, session, executor):
    from workbench.runs.store import mark_running

    project = a_project(session)
    client.post(f"/projects/{project.id}/conversation")
    run = session.query(Run).one()
    mark_running(session, run)

    page = client.get(f"/runs/{run.id}").text

    assert f"{project.owner}/{project.repo}" in page
    assert f"/projects/{project.id}" in page
    assert ">Send<" in page
