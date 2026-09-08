"""Which agent works a project, and where its work goes when the window runs out.

The failover is the part worth pinning. It moves real work between machines
that are not equally good at it, so what matters is that it moves only on the
backend's own word, says so where the run records it, and never moves a
conversation — a resume token means nothing to a backend that did not issue it,
and continuing somewhere else would look exactly like the agent forgetting
everything.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from workbench.database.db import make_engine
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
from workbench.runs.lifecycle import Chosen, choose_backend
from workbench.runs.store import append_event, create_run, finish_run


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "test.db"))
    monkeypatch.delenv("WORKBENCH_AGENT_BACKEND", raising=False)
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def project(db) -> Project:
    item = Project(user=User(name="ian"), owner="idm23", repo="workbench", github_url="u")
    db.add(Task(project=item, title="Add a healthz endpoint"))
    db.commit()
    return item


def a_limit(db, project, *, backend: str, status: str = "rejected", resets_in_hours: int = 3):
    """A window reading, reported by a run of `backend` as one really is."""
    task = db.query(Task).filter_by(project_id=project.id).first()
    run = create_run(db, task, RunPhase.EXECUTE, backend=backend)
    finish_run(db, run, RunStatus.SUCCEEDED)
    resets_at = (datetime.now(UTC) + timedelta(hours=resets_in_hours)).timestamp()
    append_event(
        db,
        run.id,
        RunEventKind.NOTICE,
        {
            "text": "Rate limit",
            "rate_limit": {
                "status": status,
                "type": "five_hour",
                "utilization": 1.0,
                "resets_at": resets_at,
            },
        },
    )
    return run


def test_with_no_fallback_nothing_moves(db, project):
    """Waiting for a window is a perfectly reasonable thing to want, and the
    two backends are not interchangeable."""
    project.agent_backend = "claude"
    a_limit(db, project, backend="claude")

    assert choose_backend(db, project) == Chosen("claude")


def test_a_spent_window_sends_the_run_to_the_fallback(db, project):
    project.agent_backend = "claude"
    project.fallback_backend = "local"
    a_limit(db, project, backend="claude")

    picked = choose_backend(db, project)

    assert picked.backend == "local"
    assert picked.reason is not None
    assert "5-hour limit" in picked.reason
    assert "Running on local" in picked.reason


def test_the_reason_says_when_the_window_comes_back(db, project):
    project.agent_backend = "claude"
    project.fallback_backend = "local"
    a_limit(db, project, backend="claude", resets_in_hours=3)

    reason = choose_backend(db, project).reason

    assert reason is not None
    assert "back in" in reason


def test_an_unspent_window_stays_where_it_was(db, project):
    project.agent_backend = "claude"
    project.fallback_backend = "local"
    a_limit(db, project, backend="claude", status="allowed")

    assert choose_backend(db, project).backend == "claude"


def test_only_the_backends_own_word_moves_work(db, project):
    """`allowed_warning` is the backend saying it is close, not that it is
    done. Moving on a threshold of ours would move work on a guess."""
    project.agent_backend = "claude"
    project.fallback_backend = "local"
    a_limit(db, project, backend="claude", status="allowed_warning")

    assert choose_backend(db, project).backend == "claude"


def test_a_window_that_has_since_reset_does_not_move_anything(db, project):
    project.agent_backend = "claude"
    project.fallback_backend = "local"
    a_limit(db, project, backend="claude", resets_in_hours=-2)

    assert choose_backend(db, project).backend == "claude"


def test_another_backends_limit_is_not_this_ones(db, project):
    """A Claude window says nothing about a GPU. Treating one machine's limit
    as another's would move work for no reason at all."""
    project.agent_backend = "claude"
    project.fallback_backend = "local"
    a_limit(db, project, backend="local")

    assert choose_backend(db, project).backend == "claude"


def test_a_fallback_this_machine_does_not_have_is_ignored(db, project, caplog):
    project.agent_backend = "claude"
    project.fallback_backend = "gpt-9"
    a_limit(db, project, backend="claude")

    with caplog.at_level("WARNING"):
        assert choose_backend(db, project).backend == "claude"

    assert "gpt-9" in caplog.text


def test_a_backend_cannot_fall_back_to_itself(db, project):
    project.agent_backend = "claude"
    project.fallback_backend = "claude"
    a_limit(db, project, backend="claude")

    assert choose_backend(db, project) == Chosen("claude")


def test_the_machine_default_is_what_gets_moved_when_nothing_is_chosen(db, project, monkeypatch):
    monkeypatch.setenv("WORKBENCH_AGENT_BACKEND", "claude")
    project.fallback_backend = "local"
    a_limit(db, project, backend="claude")

    assert choose_backend(db, project).backend == "local"


# --- What the page and the run record ---------------------------------------


def test_a_moved_run_says_so_in_its_own_events(db, project, monkeypatch, tmp_path):
    """A run that quietly went somewhere else is a result someone will later
    try to explain from the wrong premise."""
    from workbench.runs import lifecycle
    from workbench.runs.lifecycle import start_run

    project.agent_backend = "claude"
    project.fallback_backend = "local"
    a_limit(db, project, backend="claude")
    task = db.query(Task).filter_by(project_id=project.id).first()

    monkeypatch.setattr(lifecycle, "_launch", lambda db, run, executor: run)
    started = start_run(db, task, RunPhase.EXECUTE)

    assert isinstance(started, Run)
    assert started.backend == "local"
    notices = [
        event.payload.get("text", "")
        for event in db.query(RunEvent).filter_by(run_id=started.id).all()
    ]
    assert any("Running on local" in text for text in notices)


def test_an_explicitly_chosen_backend_is_never_second_guessed(db, project, monkeypatch):
    """Asking for one is a decision, not a preference to be improved on."""
    from workbench.runs import lifecycle
    from workbench.runs.lifecycle import start_run

    project.agent_backend = "claude"
    project.fallback_backend = "local"
    a_limit(db, project, backend="claude")
    task = db.query(Task).filter_by(project_id=project.id).first()

    monkeypatch.setattr(lifecycle, "_launch", lambda db, run, executor: run)
    started = start_run(db, task, RunPhase.EXECUTE, backend="claude")

    assert isinstance(started, Run)
    assert started.backend == "claude"


def test_continuing_a_run_never_moves_it(db, project, monkeypatch):
    """A resume token means nothing to a backend that did not issue it, so
    failing over mid-conversation would start cold in the same worktree and
    look, from outside, exactly like the agent forgetting everything."""
    from workbench.runs import lifecycle
    from workbench.runs.lifecycle import continue_run

    project.agent_backend = "claude"
    project.fallback_backend = "local"
    a_limit(db, project, backend="claude")
    task = db.query(Task).filter_by(project_id=project.id).first()
    source = create_run(db, task, RunPhase.EXECUTE, backend="claude")
    finish_run(db, source, RunStatus.SUCCEEDED, resume_token="session-abc")

    monkeypatch.setattr(lifecycle, "_launch", lambda db, run, executor: run)
    continued = continue_run(db, source, message="carry on")

    assert isinstance(continued, Run)
    assert continued.backend == "claude"
