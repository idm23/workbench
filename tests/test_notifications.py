"""Telling a person something happened.

Nothing here sends a real push. What is worth pinning is the decisions either
side of the send: which transitions are worth an interruption, which devices
hear about them, and what happens when a push fails — because the one thing a
notification must never do is affect the run it is about.
"""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from workbench import notifications
from workbench.database.db import make_engine
from workbench.database.models import (
    Base,
    DeviceSubscription,
    Project,
    RunPhase,
    RunStatus,
    Task,
    User,
)
from workbench.notifications import (
    ALL_KINDS,
    RUN_FINISHED,
    RUN_NEEDS_YOU,
    about_run,
    devices_for,
    forget,
    set_enabled,
    subscribe,
)
from workbench.runs.store import create_run, finish_run


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    """A machine that has been through the installer. Without this every send
    is skipped, which would make most of these pass for the wrong reason."""
    monkeypatch.setenv("WORKBENCH_VAPID_PRIVATE_KEY", "a-private-key")
    monkeypatch.setenv("WORKBENCH_VAPID_PUBLIC_KEY", "a-public-key")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "test.db"))
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def user(db) -> User:
    person = User(name="ian")
    db.add(person)
    db.commit()
    return person


@pytest.fixture
def task(db, user) -> Task:
    project = Project(user=user, owner="idm23", repo="workbench", github_url="u")
    item = Task(project=project, title="Add a healthz endpoint")
    db.add(item)
    db.commit()
    return item


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    """Every push that would have gone out."""
    posted: list[dict] = []
    monkeypatch.setattr(
        notifications,
        "_send",
        lambda device, payload: posted.append({"device": device, "payload": payload}) or True,
    )
    return posted


def a_device(
    db,
    user,
    *,
    endpoint: str = "https://push.example/abc",
    label: str = "iPhone",
    event_kinds: list[str] | None = None,
) -> DeviceSubscription:
    return subscribe(
        db,
        user,
        endpoint=endpoint,
        p256dh="key",
        auth="secret",
        label=label,
        event_kinds=event_kinds,
    )


# --- Which transitions are worth an interruption ----------------------------


def test_a_question_is_something_only_you_can_unblock(db, task):
    run = create_run(db, task, RunPhase.EXECUTE, backend="claude")
    run.outcome_detail = "Should this replace the old endpoint?"
    finish_run(db, run, RunStatus.AWAITING_ANSWER)

    decided = about_run(run)

    assert decided is not None
    kind, title, body = decided

    assert kind == RUN_NEEDS_YOU
    assert "asked a question" in title
    assert body == "Should this replace the old endpoint?"


def test_a_plan_waiting_to_be_read_is_too(db, task):
    run = create_run(db, task, RunPhase.PLAN, backend="claude")
    finish_run(db, run, RunStatus.AWAITING_REVIEW)

    decided = about_run(run)

    assert decided is not None
    kind, title, _ = decided

    assert kind == RUN_NEEDS_YOU
    assert "ready to read" in title


def test_a_finished_run_is_news_rather_than_a_summons(db, task):
    run = create_run(db, task, RunPhase.EXECUTE, backend="claude")
    finish_run(db, run, RunStatus.SUCCEEDED, summary="Added the endpoint and a test.")

    decided = about_run(run)

    assert decided is not None
    kind, title, body = decided

    assert kind == RUN_FINISHED
    assert "done" in title
    assert "Added the endpoint" in body


def test_a_failure_says_why(db, task):
    run = create_run(db, task, RunPhase.EXECUTE, backend="claude")
    finish_run(db, run, RunStatus.FAILED, error="Tests fail and I could not find why.")

    decided = about_run(run)

    assert decided is not None
    kind, _, body = decided

    assert kind == RUN_FINISHED
    assert "Tests fail" in body


def test_cancelling_says_nothing(db, task):
    """You pressed the button. Being told is being told what you just did."""
    run = create_run(db, task, RunPhase.EXECUTE, backend="claude")
    finish_run(db, run, RunStatus.CANCELLED)

    assert about_run(run) is None


# --- Who hears about it -----------------------------------------------------


def test_a_run_ending_notifies_the_owner(db, task, user, sent):
    a_device(db, user)
    run = create_run(db, task, RunPhase.EXECUTE, backend="claude")

    finish_run(db, run, RunStatus.SUCCEEDED, summary="Done.")

    assert len(sent) == 1
    assert "Add a healthz endpoint" in sent[0]["payload"]
    assert f'"url": "/runs/{run.id}"' in sent[0]["payload"]


def test_a_silenced_device_hears_nothing(db, task, user, sent):
    set_enabled(db, a_device(db, user), False)
    run = create_run(db, task, RunPhase.EXECUTE, backend="claude")

    finish_run(db, run, RunStatus.SUCCEEDED)

    assert sent == []


def test_a_device_can_want_only_the_interruptions(db, task, user, sent):
    """The reason kinds are a list rather than a flag: a phone worth waking for
    a question is not necessarily worth waking for every finished run."""
    a_device(db, user, event_kinds=[RUN_NEEDS_YOU])
    run = create_run(db, task, RunPhase.EXECUTE, backend="claude")

    finish_run(db, run, RunStatus.SUCCEEDED)
    assert sent == []

    other = create_run(db, task, RunPhase.EXECUTE, backend="claude")
    finish_run(db, other, RunStatus.AWAITING_ANSWER)
    assert len(sent) == 1


def test_another_persons_devices_are_not_told(db, task, user, sent):
    stranger = User(name="someone-else")
    db.add(stranger)
    db.commit()
    a_device(db, stranger, endpoint="https://push.example/theirs")
    run = create_run(db, task, RunPhase.EXECUTE, backend="claude")

    finish_run(db, run, RunStatus.SUCCEEDED)

    assert sent == []


def test_a_machine_with_no_keys_sends_nothing(db, task, user, sent, monkeypatch):
    monkeypatch.delenv("WORKBENCH_VAPID_PRIVATE_KEY")
    a_device(db, user)
    run = create_run(db, task, RunPhase.EXECUTE, backend="claude")

    finish_run(db, run, RunStatus.SUCCEEDED)

    assert sent == []


# --- Subscribing ------------------------------------------------------------


def test_subscribing_twice_from_one_device_is_one_device(db, user):
    """A browser hands back the same endpoint, and two rows would mean two
    notifications for one event."""
    a_device(db, user)
    a_device(db, user, label="iPhone 15")

    devices = devices_for(db, user.id)

    assert len(devices) == 1
    assert devices[0].label == "iPhone 15"


def test_a_new_subscription_wants_everything_by_default(db, user):
    assert set(a_device(db, user).event_kinds) == set(ALL_KINDS)


def test_re_subscribing_clears_a_previous_failure(db, user):
    device = a_device(db, user)
    device.enabled = False
    device.last_error = "gone"
    db.commit()

    revived = a_device(db, user)

    assert revived.enabled
    assert revived.last_error is None


def test_forgetting_a_device_removes_it(db, user):
    forget(db, a_device(db, user))

    assert devices_for(db, user.id) == []


# --- When a push fails ------------------------------------------------------


def test_a_failing_push_never_reaches_the_run(db, task, user, monkeypatch):
    """The one thing this must never do. A person not being told is worth a log
    line; it is not worth failing the run that was trying to tell them."""

    def explode(device, payload):
        raise RuntimeError("the push service is on fire")

    monkeypatch.setattr(notifications, "_send", explode)
    a_device(db, user)
    run = create_run(db, task, RunPhase.EXECUTE, backend="claude")

    finish_run(db, run, RunStatus.SUCCEEDED, summary="Done.")

    assert run.status is RunStatus.SUCCEEDED
    assert run.summary == "Done."


def test_a_gone_subscription_is_disabled_rather_than_deleted(db, user, monkeypatch):
    """Only the push service knows a browser threw its subscription away.
    Disabled keeps the row, and the reason, in the list."""

    class Response:
        status_code = 410

    class GoneError(Exception):
        response = Response()

    import pywebpush

    monkeypatch.setattr(pywebpush, "WebPushException", GoneError)

    def refuse(**kwargs):
        raise GoneError("gone")

    monkeypatch.setattr(pywebpush, "webpush", refuse)
    device = a_device(db, user)

    assert notifications._send(device, "{}") is False
    assert not device.enabled
    assert device.last_error is not None
    assert "gone" in device.last_error


def test_the_default_subject_is_one_a_push_service_will_accept(monkeypatch):
    """Found by sending a real push rather than a stubbed one: the library
    requires a `sub` that could route, so `mailto:workbench@some-hostname` is
    refused — and the error names neither the setting nor the reason."""
    from py_vapid import _check_sub

    from workbench.config import vapid_subject

    monkeypatch.delenv("WORKBENCH_VAPID_SUBJECT", raising=False)

    assert _check_sub(vapid_subject())


def test_a_configured_subject_wins(monkeypatch):
    monkeypatch.setenv("WORKBENCH_VAPID_SUBJECT", "mailto:ian@example.com")

    from workbench.config import vapid_subject

    assert vapid_subject() == "mailto:ian@example.com"


# --- Serving the worker ------------------------------------------------------


def test_the_service_worker_is_served_from_the_root(tmp_path, monkeypatch):
    """Its scope is the directory it is served from. At `/static/sw.js` it
    could only ever control `/static/…`, so subscribing from a user page waited
    on a worker that would never control it — the button stayed disabled and
    nothing said why."""
    from fastapi.testclient import TestClient

    from workbench.app import app

    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "sw.db"))
    with TestClient(app) as client:
        response = client.get("/sw.js")

    assert response.status_code == 200
    assert "text/javascript" in response.headers["content-type"]
    assert "showNotification" in response.text


def test_the_subscription_script_registers_the_root_worker():
    """Pinned because the failure is silent: a worker registered under
    `/static` installs perfectly and then controls nothing that matters."""
    source = (
        Path(__file__).resolve().parent.parent / "src/workbench/static/notifications.js"
    ).read_text()
    # Comments stripped: one of them explains why `ready` is not used, and a
    # test that cannot tell an explanation from a call is one that fails for
    # being right about it.
    script = "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))

    assert 'register("/sw.js")' in script
    assert "serviceWorker.ready" not in script


# --- The token this machine signs with --------------------------------------


def test_the_token_expires_inside_apples_window():
    """Apple requires *no more than* 24 hours, `py_vapid` asks for exactly
    that, and this machine's clock was measured 2.4 seconds fast — so every
    push came back `403 BadJwtToken`, a four-word reason for a two-second
    problem. Tested here rather than discovered there."""
    import time

    from workbench.notifications import vapid_claims

    lifetime = vapid_claims()["exp"] - int(time.time())

    assert 0 < lifetime < 24 * 60 * 60
    # And with real margin, not by a second: latency and future clock drift
    # both eat into it.
    assert lifetime <= 12 * 60 * 60


def test_the_token_says_who_is_sending():
    from workbench.notifications import vapid_claims

    assert str(vapid_claims()["sub"]).startswith("mailto:")


def test_a_test_push_reports_whether_it_worked(db, user, monkeypatch):
    """The point of the button: an answer now, rather than after a run."""
    from workbench.notifications import send_test

    device = a_device(db, user)
    monkeypatch.setattr(notifications, "_send", lambda d, payload: True)

    assert send_test(db, device) is True


def test_a_failing_test_push_says_so(db, user, monkeypatch):
    from workbench.notifications import send_test

    device = a_device(db, user)
    monkeypatch.setattr(notifications, "_send", lambda d, payload: False)

    assert send_test(db, device) is False
