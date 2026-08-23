"""The two things the task page says about work in progress.

Both answer a question someone asks from a phone without touching anything: is
an agent working on this right now, and how much of the account's rate-limit
window is left. Neither is derivable from the task rows, and both are the kind
of thing that quietly stops rendering when the data shape underneath it moves.
"""

from datetime import UTC, datetime, timedelta

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
    User,
)


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
        db.add(Task(project=project, title="Something else"))
        db.commit()
        yield db
    app.dependency_overrides.clear()


@pytest.fixture
def client(session):
    return TestClient(app)


def a_rate_limit_event(
    db,
    *,
    window: str = "five_hour",
    status: str = "allowed",
    utilization: float | None = 0.4,
    hours: int = 2,
):
    """A notice shaped the way the Claude adapter writes one.

    `utilization` is passed as None to model a real reading that omits it —
    which the one recorded on this machine does.
    """
    task = db.query(Task).first()
    run = Run(task_id=task.id, phase=RunPhase.EXECUTE, backend="claude", status=RunStatus.SUCCEEDED)
    db.add(run)
    db.commit()
    db.add(
        RunEvent(
            run_id=run.id,
            seq=1,
            kind=RunEventKind.NOTICE,
            payload={
                "text": f"Rate limit {status} ({window}).",
                "rate_limit": {
                    "status": status,
                    "type": window,
                    "utilization": utilization,
                    "resets_at": int((datetime.now(UTC) + timedelta(hours=hours)).timestamp()),
                },
            },
        )
    )
    db.commit()
    return run


def a_run(db, *, title="Write the runner", status=RunStatus.RUNNING, phase=RunPhase.EXECUTE):
    task = db.query(Task).filter_by(title=title).one()
    run = Run(task_id=task.id, phase=phase, backend="claude", status=status)
    db.add(run)
    db.commit()
    return run


def _squashed(page: str) -> str:
    """Collapse whitespace, so an assertion is not about template indentation."""
    return " ".join(page.split())


def project_page(client, session) -> str:
    project = session.query(Project).one()
    response = client.get(f"/projects/{project.id}")
    assert response.status_code == 200
    return response.text


# --- Rate limits -----------------------------------------------------------


def test_the_panel_is_there_before_any_run_has_reported(client, session):
    """Someone checks this *before* starting a run, so it cannot appear only after."""
    page = project_page(client, session)

    assert "Rate limits" in page
    assert "No reading yet" in page


def test_a_reading_is_shown_as_a_percentage(client, session):
    a_rate_limit_event(session, utilization=0.42)

    page = project_page(client, session)

    assert "42% used" in page
    assert "5-hour" in page


def test_the_reset_time_is_shown(client, session):
    a_rate_limit_event(session, hours=2)

    assert "resets in" in project_page(client, session)


def test_a_window_near_its_limit_is_marked(client, session):
    a_rate_limit_event(session, utilization=0.9)

    assert 'class="limit warning"' in project_page(client, session)


def test_a_rejected_window_is_marked_more_strongly(client, session):
    """The backend saying `rejected` beats any threshold of ours."""
    a_rate_limit_event(session, status="rejected", utilization=1.0)

    assert 'class="limit exhausted"' in project_page(client, session)


def test_only_the_newest_reading_for_a_window_is_shown(client, session):
    a_rate_limit_event(session, utilization=0.2)
    a_rate_limit_event(session, utilization=0.8)

    page = project_page(client, session)

    assert "80% used" in page
    assert "20% used" not in page


def test_separate_windows_are_shown_separately(client, session):
    a_rate_limit_event(session, window="five_hour", utilization=0.3)
    a_rate_limit_event(session, window="seven_day", utilization=0.6)

    page = project_page(client, session)

    assert "5-hour" in page
    assert "7-day" in page


def test_the_panel_appears_on_the_other_pages_too(client, session):
    """The window belongs to the account, so it is not a property of one project."""
    a_rate_limit_event(session, utilization=0.5)

    assert "Rate limits" in client.get("/").text
    assert "Rate limits" in client.get(f"/users/{session.query(User).one().id}").text


def test_an_ordinary_notice_is_not_mistaken_for_a_reading(client, session):
    task = session.query(Task).first()
    run = Run(task_id=task.id, phase=RunPhase.PLAN, backend="claude", status=RunStatus.SUCCEEDED)
    session.add(run)
    session.commit()
    session.add(
        RunEvent(run_id=run.id, seq=1, kind=RunEventKind.NOTICE, payload={"text": "Just a notice."})
    )
    session.commit()

    assert "No reading yet" in project_page(client, session)


# --- Tasks being worked ----------------------------------------------------


def test_a_task_with_no_run_carries_no_marker(client, session):
    page = project_page(client, session)

    assert 'class="pip"' not in page
    assert "</span>" in page  # the page did render badges, just not an activity one


def test_a_running_execute_run_marks_its_task(client, session):
    a_run(session, status=RunStatus.RUNNING, phase=RunPhase.EXECUTE)

    page = project_page(client, session)

    assert "working</a>" in _squashed(page)
    assert 'class="pip"' in page


def test_a_running_plan_run_says_planning_instead(client, session):
    """The phase is the part a person cares about."""
    a_run(session, status=RunStatus.RUNNING, phase=RunPhase.PLAN)

    assert "planning</a>" in _squashed(project_page(client, session))


def test_a_queued_run_is_marked_but_not_as_live(client, session):
    a_run(session, status=RunStatus.QUEUED)

    page = project_page(client, session)

    assert "queued</a>" in _squashed(page)
    assert 'class="status live"' not in page


def test_a_plan_waiting_on_a_person_is_marked(client, session):
    """The state most easily forgotten: nothing else on the page says so."""
    a_run(session, status=RunStatus.AWAITING_REVIEW, phase=RunPhase.PLAN)

    assert "review</a>" in _squashed(project_page(client, session))


def test_a_finished_run_leaves_no_marker(client, session):
    a_run(session, status=RunStatus.SUCCEEDED)

    assert 'class="pip"' not in project_page(client, session)


def test_only_the_task_being_worked_is_marked(client, session):
    a_run(session, title="Write the runner", status=RunStatus.RUNNING)

    page = project_page(client, session)

    assert page.count('class="pip"') == 1


def test_the_newest_run_decides_what_is_shown(client, session):
    a_run(session, status=RunStatus.AWAITING_REVIEW, phase=RunPhase.PLAN)
    a_run(session, status=RunStatus.RUNNING, phase=RunPhase.EXECUTE)

    assert "working</a>" in _squashed(project_page(client, session))


def test_the_marker_carries_a_word_and_not_only_a_colour(client, session):
    """Colour alone is not an indicator someone can necessarily read."""
    a_run(session, status=RunStatus.RUNNING)

    assert "working</a>" in _squashed(project_page(client, session))


def test_a_reading_with_no_percentage_still_says_something(client, session):
    """`utilization` is optional in the protocol, and really is absent sometimes.

    The one rate-limit record on this machine carries a status, a window, and a
    reset time with no number at all. Showing the raw `allowed` there would be
    worse than useless.
    """
    a_rate_limit_event(session, utilization=None, status="rejected")

    page = project_page(client, session)

    assert "limit reached" in page
    assert 'role="meter"' not in page


def test_a_reading_with_no_percentage_still_marks_the_level(client, session):
    """The backend's own status is enough to colour it, with or without a number."""
    a_rate_limit_event(session, utilization=None, status="allowed_warning")

    assert 'class="limit warning"' in project_page(client, session)


def test_the_panel_says_how_old_the_reading_is(client, session):
    """These refresh only when something talks to the backend, so age is the point."""
    a_rate_limit_event(session)

    assert "as of" in project_page(client, session)
