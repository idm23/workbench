"""What the services page shows: this instance's own units, and any shell
command an active run has not yet gotten a result back for.

`systemctl` is never actually invoked here — the test sandbox has no
systemd, so `systemd_available()` is already false, and the one test that
needs the systemd branch monkeypatches both that and the subprocess call
rather than touching a real unit.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import workbench.services as services_module
from workbench.app import app
from workbench.database.db import get_db, make_engine
from workbench.database.models import (
    Base,
    Project,
    Run,
    RunEventKind,
    RunPhase,
    RunStatus,
    Task,
    User,
)
from workbench.runs.executors import SYSTEMD_EXECUTOR
from workbench.runs.store import append_event
from workbench.services import ServiceUnit, active_shells, running_services


@pytest.fixture(autouse=True)
def no_real_systemd(monkeypatch):
    """This box may well have a real systemd, unlike the CI container the
    rest of the suite assumes — force the no-systemd path by default so a
    test is never quietly deciding its own outcome from the machine it
    happens to run on. The handful of tests that want the systemd branch
    turn it back on explicitly."""
    monkeypatch.setattr(services_module, "systemd_available", lambda: False)
    monkeypatch.setattr("workbench.app.systemd_available", lambda: False)


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
        task = Task(project=project, title="Write the runner")
        db.add(task)
        db.commit()
        yield db
    app.dependency_overrides.clear()


def a_task(session) -> Task:
    return session.query(Task).one()


def a_project(session) -> Project:
    return session.query(Project).one()


# --- running_services --------------------------------------------------------


def test_nothing_running_is_an_empty_list(session):
    assert running_services(session) == []


def test_an_active_run_shows_up_without_systemd(session):
    """No systemd on this machine, but a run holding a concurrency slot is
    still worth showing — it is a real thing happening right now."""
    task = a_task(session)
    run = Run(task_id=task.id, phase=RunPhase.EXECUTE, backend="claude", status=RunStatus.RUNNING)
    run.executor = "local-process"
    run.handle = "4321"
    session.add(run)
    session.commit()

    found = running_services(session)

    assert len(found) == 1
    assert found[0].unit == "4321"
    assert found[0].active is True
    assert "Write the runner" in found[0].label
    assert found[0].run is not None
    assert found[0].run.id == run.id


def test_a_finished_run_is_not_a_service(session):
    task = a_task(session)
    run = Run(task_id=task.id, phase=RunPhase.EXECUTE, backend="claude", status=RunStatus.SUCCEEDED)
    session.add(run)
    session.commit()

    assert running_services(session) == []


def test_a_project_conversation_is_labelled_by_the_project(session):
    project = a_project(session)
    run = Run(
        project_id=project.id,
        phase=RunPhase.CONVERSATION,
        backend="claude",
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.commit()

    found = running_services(session)

    assert len(found) == 1
    assert found[0].label == "Conversation: idm23/workbench"


def test_systemd_units_are_read_when_the_machine_has_one(session, monkeypatch):
    """The static app/deploy units and a systemd-executed run's own unit all
    come from one batched `systemctl show` — this stubs that call rather
    than touching a real unit."""
    task = a_task(session)
    run = Run(task_id=task.id, phase=RunPhase.EXECUTE, backend="claude", status=RunStatus.RUNNING)
    run.executor = SYSTEMD_EXECUTOR
    run.handle = "workbench-run@1.service"
    session.add(run)
    session.commit()

    monkeypatch.setattr(services_module, "systemd_available", lambda: True)
    monkeypatch.setattr(services_module, "service_name", lambda: "workbench")
    monkeypatch.setattr(services_module, "deploy_unit_name", lambda: "workbench-deploy")

    def fake_show(units: list[str]) -> dict[str, tuple[bool, str]]:
        return {
            "workbench.service": (True, "active (running)"),
            "workbench-deploy.service": (False, "inactive (dead)"),
            "workbench-deploy.timer": (True, "active (waiting)"),
            "workbench-run@1.service": (True, "active (running)"),
        }

    monkeypatch.setattr(services_module, "_show", fake_show)

    found = running_services(session)

    units = {s.unit: s for s in found}
    assert units["workbench.service"].active is True
    assert units["workbench-deploy.timer"].active is True
    run_service = units["workbench-run@1.service"]
    assert run_service.run is not None
    assert run_service.run.id == run.id
    # Handled once, by its real unit status — not duplicated by the
    # no-systemd fallback loop.
    assert len(found) == 4


def test_a_run_started_before_systemd_shows_unknown_rather_than_erroring(session, monkeypatch):
    """`systemctl show` on a unit it has never heard of answers with nothing
    for that block, not an error — a stale handle must not crash the page."""
    task = a_task(session)
    run = Run(task_id=task.id, phase=RunPhase.EXECUTE, backend="claude", status=RunStatus.RUNNING)
    run.executor = SYSTEMD_EXECUTOR
    run.handle = "workbench-run@999.service"
    session.add(run)
    session.commit()

    monkeypatch.setattr(services_module, "systemd_available", lambda: True)
    monkeypatch.setattr(services_module, "_show", lambda units: {})

    found = running_services(session)

    run_service = next(s for s in found if s.run is not None)
    assert run_service.active is False
    assert run_service.state == "unknown"


def test_status_class_reflects_state():
    active = ServiceUnit(unit="a", label="a", active=True, state="active (running)")
    failed = ServiceUnit(unit="b", label="b", active=False, state="failed (failed)")
    resting = ServiceUnit(unit="c", label="c", active=False, state="inactive (dead)")

    assert active.status_class == "active"
    assert failed.status_class == "failed"
    assert resting.status_class == ""


def test_show_parses_a_batched_systemctl_call(monkeypatch):
    """The real parser, against real `systemctl show`-shaped output —
    blocks separated by a blank line, one `Key=Value` pair per line."""
    output = (
        "Id=workbench.service\nActiveState=active\nSubState=running\n"
        "\n"
        "Id=workbench-deploy.timer\nActiveState=active\nSubState=waiting\n"
    )

    class FakeResult:
        stdout = output
        returncode = 0

    monkeypatch.setattr(services_module.subprocess, "run", lambda *a, **k: FakeResult())

    result = services_module._show(["workbench.service", "workbench-deploy.timer"])

    assert result["workbench.service"] == (True, "active (running)")
    assert result["workbench-deploy.timer"] == (True, "active (waiting)")


def test_show_with_no_units_skips_the_call_entirely(monkeypatch):
    def fail(*_a: Any, **_k: Any):
        pytest.fail("systemctl should not be invoked for an empty unit list")

    monkeypatch.setattr(services_module.subprocess, "run", fail)

    assert services_module._show([]) == {}


# --- active_shells -----------------------------------------------------------


def _running_run(session) -> Run:
    run = Run(
        task_id=a_task(session).id,
        phase=RunPhase.EXECUTE,
        backend="claude",
        status=RunStatus.RUNNING,
    )
    session.add(run)
    session.commit()
    return run


def test_an_unresolved_bash_call_is_a_shell_in_progress(session):
    run = _running_run(session)
    append_event(
        session,
        run.id,
        RunEventKind.TOOL_USE,
        {"id": "t1", "name": "Bash", "input": {"command": "pytest -q"}},
    )

    found = active_shells(session)

    assert len(found) == 1
    assert found[0].run.id == run.id
    assert found[0].command == "pytest -q"


def test_a_resolved_bash_call_is_not_shown(session):
    run = _running_run(session)
    append_event(
        session,
        run.id,
        RunEventKind.TOOL_USE,
        {"id": "t1", "name": "Bash", "input": {"command": "ls"}},
    )
    append_event(
        session, run.id, RunEventKind.TOOL_RESULT, {"id": "t1", "text": "a.py", "is_error": False}
    )

    assert active_shells(session) == []


def test_a_non_bash_tool_call_is_never_a_shell(session):
    run = _running_run(session)
    append_event(
        session,
        run.id,
        RunEventKind.TOOL_USE,
        {"id": "t1", "name": "Read", "input": {"path": "a.py"}},
    )

    assert active_shells(session) == []


def test_only_the_newest_unresolved_call_is_shown(session):
    run = _running_run(session)
    append_event(
        session,
        run.id,
        RunEventKind.TOOL_USE,
        {"id": "t1", "name": "Bash", "input": {"command": "first"}},
    )
    append_event(session, run.id, RunEventKind.TOOL_RESULT, {"id": "t1"})
    append_event(
        session,
        run.id,
        RunEventKind.TOOL_USE,
        {"id": "t2", "name": "Bash", "input": {"command": "second"}},
    )

    found = active_shells(session)

    assert len(found) == 1
    assert found[0].command == "second"


def test_a_finished_runs_events_are_never_scanned(session):
    task = a_task(session)
    run = Run(task_id=task.id, phase=RunPhase.EXECUTE, backend="claude", status=RunStatus.SUCCEEDED)
    session.add(run)
    session.commit()
    append_event(
        session,
        run.id,
        RunEventKind.TOOL_USE,
        {"id": "t1", "name": "Bash", "input": {"command": "ls"}},
    )

    assert active_shells(session) == []


def test_a_shells_label_matches_its_runs(session):
    run = _running_run(session)
    append_event(
        session,
        run.id,
        RunEventKind.TOOL_USE,
        {"id": "t1", "name": "Bash", "input": {"command": "ls"}},
    )

    assert active_shells(session)[0].label == "Task: Write the runner"


# --- The route -----------------------------------------------------------


@pytest.fixture
def client(session):
    return TestClient(app)


def test_the_services_page_renders_with_nothing_running(client):
    page = client.get("/services").text

    assert "Nothing running" in page
    assert "No shell commands in flight" in page


def test_the_services_page_lists_an_active_run(client, session):
    task = a_task(session)
    run = Run(task_id=task.id, phase=RunPhase.EXECUTE, backend="claude", status=RunStatus.RUNNING)
    run.executor = "local-process"
    run.handle = "555"
    session.add(run)
    session.commit()

    page = client.get("/services").text

    assert "Write the runner" in page
    assert f"/runs/{run.id}" in page


def test_the_services_page_shows_a_shell_in_progress(client, session):
    task = a_task(session)
    run = Run(task_id=task.id, phase=RunPhase.EXECUTE, backend="claude", status=RunStatus.RUNNING)
    session.add(run)
    session.commit()
    append_event(
        session,
        run.id,
        RunEventKind.TOOL_USE,
        {"id": "t1", "name": "Bash", "input": {"command": "pytest -q"}},
    )

    page = client.get("/services").text

    assert "pytest -q" in page


def test_the_home_page_links_to_it(client):
    assert 'href="/services"' in client.get("/").text
