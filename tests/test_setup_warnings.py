"""The banner that says what the install could not finish.

The install says these things once, into a terminal nobody re-reads, and the
state they describe changes long afterwards — a credential expires, a serve
mapping is reset. So the app says them too, on every page, until they are gone.

The assertions that matter most are the negative ones. A banner that cries wolf
when its own checker is broken is a banner people learn to scroll past, and
then they miss the real one.
"""

import json
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from workbench import doctor
from workbench.app import app
from workbench.database.db import get_db, make_engine
from workbench.database.models import Base, Project, Task, User


@pytest.fixture(autouse=True)
def no_cached_answer():
    """The TTL cache is process-wide, so tests would otherwise see each other."""
    doctor._warnings_at.cache_clear()
    yield
    doctor._warnings_at.cache_clear()


@pytest.fixture
def client(tmp_path, monkeypatch):
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
        db.refresh(project)
        identifiers = (project.user_id, project.id)
    yield TestClient(app), identifiers
    app.dependency_overrides.clear()


class FakeDoctor:
    """Stands in for the subprocess, and counts how often it was asked."""

    def __init__(self, checks, *, returncode=1, stdout=None):
        self.payload = {"instance": "production", "account": "workbench", "checks": checks}
        self.returncode = returncode
        self.stdout = stdout
        self.calls = 0

    def __call__(self, argv, **_kwargs):
        self.calls += 1
        out = self.payload if self.stdout is None else None
        return subprocess.CompletedProcess(
            argv, self.returncode, json.dumps(out) if out is not None else self.stdout, ""
        )


def a_check(key, state, **overrides):
    return {
        "key": key,
        "title": f"title for {key}",
        "state": state,
        "detail": f"detail for {key}",
        "fix": None,
        **overrides,
    }


# --- What reaches the page ----------------------------------------------------


def test_an_unauthenticated_agent_warns_on_every_page(monkeypatch, client):
    """A tree offering to start an agent that cannot authenticate is misleading
    on every page it appears on, not just on a run's."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        FakeDoctor([a_check("agent-credential", "fail", fix="claude auth login --claudeai")]),
    )
    http, (user_id, project_id) = client

    for path in ("/", f"/users/{user_id}", f"/projects/{project_id}"):
        body = http.get(path).text
        assert "title for agent-credential" in body, path
        assert "claude auth login --claudeai" in body, path


def test_nothing_outstanding_means_no_banner(monkeypatch, client):
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        FakeDoctor([a_check("agent-credential", "ok")], returncode=0),
    )
    http, _ = client

    assert 'aria-label="Setup"' not in http.get("/").text


def test_a_failure_and_a_warning_are_styled_apart(monkeypatch, client):
    """ "Nobody can reach this from a phone" and "no agent can run" are not the
    same size of problem."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        FakeDoctor(
            [
                a_check("agent-credential", "fail"),
                a_check("tailscale-serve", "warn"),
            ]
        ),
    )
    http, _ = client

    body = http.get("/").text
    assert "setup-item urgent" in body
    assert "setup-item todo" in body


def test_findings_that_break_a_run_do_not_take_over_every_page(monkeypatch, client):
    """A missing git identity breaks a run, and a run is where it is said."""
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        FakeDoctor([a_check("git-identity", "fail"), a_check("deploy-key", "fail")]),
    )
    http, _ = client

    assert 'aria-label="Setup"' not in http.get("/").text


def test_the_rate_limit_panel_is_still_there(monkeypatch, client):
    """It lost its suppression flag in this change; it must not have lost the
    panel with it."""
    monkeypatch.setattr(doctor.subprocess, "run", FakeDoctor([], returncode=0))
    http, _ = client

    assert 'aria-label="Rate limits"' in http.get("/").text


# --- Not crying wolf ----------------------------------------------------------


def test_a_broken_checker_shows_nothing_rather_than_something_alarming(monkeypatch, client):
    monkeypatch.setattr(doctor.subprocess, "run", FakeDoctor([], stdout="not json at all"))
    http, _ = client

    assert 'aria-label="Setup"' not in http.get("/").text


def test_a_probe_that_cannot_run_shows_nothing(monkeypatch, client):
    def explode(*_args, **_kwargs):
        raise OSError("no such interpreter")

    monkeypatch.setattr(doctor.subprocess, "run", explode)
    http, _ = client

    assert http.get("/").status_code == 200
    assert 'aria-label="Setup"' not in http.get("/").text


def test_a_wedged_probe_does_not_hold_the_page_open(monkeypatch, client):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="doctor", timeout=15)

    monkeypatch.setattr(doctor.subprocess, "run", timeout)
    http, _ = client

    assert http.get("/").status_code == 200


def test_an_unknown_verdict_raises_no_banner(monkeypatch, client):
    """No tailscale on this machine is not a misconfiguration of this machine."""
    monkeypatch.setattr(
        doctor.subprocess, "run", FakeDoctor([a_check("tailscale-serve", "unknown")])
    )
    http, _ = client

    assert 'aria-label="Setup"' not in http.get("/").text


def test_the_doctors_own_exit_code_is_not_read_as_failure(monkeypatch):
    """It exits 1 whenever anything failed — which is exactly when there is
    something to show. Treating non-zero as unreadable would blank the banner
    precisely when it was needed."""
    monkeypatch.setattr(
        doctor.subprocess, "run", FakeDoctor([a_check("agent-credential", "fail")], returncode=1)
    )

    assert len(doctor.page_warnings()) == 1


# --- The cache ----------------------------------------------------------------


def test_two_page_loads_inside_the_window_ask_once(monkeypatch, client):
    """One subprocess per minute, not one per request. Four routes render this
    and a phone reloads freely."""
    fake = FakeDoctor([a_check("agent-credential", "fail")])
    monkeypatch.setattr(doctor.subprocess, "run", fake)
    http, _ = client

    http.get("/")
    http.get("/")

    assert fake.calls == 1


def test_the_answer_expires(monkeypatch):
    """Cached for the life of a bucket, not the life of the process — the whole
    point is that somebody goes and fixes it while this keeps running."""
    fake = FakeDoctor([a_check("agent-credential", "fail")])
    monkeypatch.setattr(doctor.subprocess, "run", fake)
    clock = iter([0.0, doctor.PAGE_STATUS_TTL_SECONDS + 1.0])
    monkeypatch.setattr(doctor.time, "monotonic", lambda: next(clock))

    doctor.page_warnings()
    doctor.page_warnings()

    assert fake.calls == 2
