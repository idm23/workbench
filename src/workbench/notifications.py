"""Telling a person something happened, on whatever device they said.

Web Push rather than a relay or a Tailscale device list: the browser already
knows what a device is, and it hands over an endpoint that works from anywhere
this machine can make an outbound request. That matters here more than usual —
this server has no public ingress by design, and a push is a POST *out* to the
browser vendor's push service, so nothing about it reopens that decision.

One delivery mechanism, deliberately, and not a seam. The agent backend earned
its adapter because a second vendor was a stated intention; there is no second
notification transport anyone has asked for, and building the shape for one
would be inventing a requirement. What the task actually asks for is that a new
*trigger* is a new call site, which is what `notify` being generic gives.

Nothing here raises. A push that fails is a person not being told, which is
worth a log line and worth recording against the device — never worth failing
the run that was trying to say something.
"""

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.config import (
    notifications_configured,
    vapid_private_key,
    vapid_subject,
)
from workbench.database.models import DeviceSubscription, Run, RunStatus, User

logger = logging.getLogger(__name__)

#: What a device can ask to hear about. Plain strings, stored in a JSON list,
#: so a new kind is a new value rather than a migration.
#:
#: Two rather than one because they are different interruptions. A finished run
#: is news; a run that stopped to ask, or a plan waiting to be read, is a thing
#: only you can unblock — and someone may reasonably want the second on a phone
#: and neither on a laptop.
RUN_FINISHED = "run_finished"
RUN_NEEDS_YOU = "run_needs_you"
ALL_KINDS = (RUN_FINISHED, RUN_NEEDS_YOU)

#: How long to wait on a push service. Short: this runs inside `finish_run`,
#: which is on the path of every run ending, and a slow push service must not
#: hold that up.
SEND_TIMEOUT_SECONDS = 10

#: Status codes that mean the subscription is gone for good. The browser threw
#: it away — cleared site data, reinstalled, revoked permission — and the push
#: service is the only thing that knows. Anything else is treated as temporary.
GONE_STATUSES = (404, 410)


def subscribe(
    db: Session,
    user: User,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    label: str,
    event_kinds: list[str] | None = None,
) -> DeviceSubscription:
    """Record a device's subscription, or update the one it already had.

    Keyed on the endpoint, because that is what a browser hands back: pressing
    the button twice on one device is the same device, and a second row would
    mean two notifications for one event.
    """
    existing = db.execute(
        select(DeviceSubscription).where(DeviceSubscription.endpoint == endpoint)
    ).scalar_one_or_none()

    device = existing or DeviceSubscription(endpoint=endpoint)
    device.user_id = user.id
    device.p256dh = p256dh
    device.auth = auth
    device.label = label or "This device"
    device.event_kinds = list(event_kinds or ALL_KINDS)
    device.enabled = True
    device.last_error = None
    if existing is None:
        db.add(device)
    db.commit()
    db.refresh(device)
    return device


def devices_for(db: Session, user_id: int) -> list[DeviceSubscription]:
    return list(
        db.execute(
            select(DeviceSubscription)
            .where(DeviceSubscription.user_id == user_id)
            .order_by(DeviceSubscription.id)
        )
        .scalars()
        .all()
    )


def set_enabled(db: Session, device: DeviceSubscription, enabled: bool) -> DeviceSubscription:
    device.enabled = enabled
    db.commit()
    return device


def forget(db: Session, device: DeviceSubscription) -> None:
    db.delete(device)
    db.commit()


def notify(
    db: Session,
    user_id: int,
    *,
    kind: str,
    title: str,
    body: str,
    url: str | None = None,
) -> int:
    """Tell every device of this user that wants this kind. Returns how many
    were sent.

    The generic entry point, and the extensibility the task asks for: a new
    trigger is a call to this, not a new anything.
    """
    if not notifications_configured():
        # Not a warning. An install that never generated keys is one that never
        # wanted notifications, and saying so on every run would be noise.
        return 0

    devices = [
        device
        for device in devices_for(db, user_id)
        if device.enabled and kind in (device.event_kinds or [])
    ]
    payload = json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    for device in devices:
        if _send(device, payload):
            sent += 1
    db.commit()
    return sent


def _send(device: DeviceSubscription, payload: str) -> bool:
    """One push. Never raises; records what happened on the device."""
    # Imported here rather than at module scope so that importing this module
    # — which `runs.store` does, on the path of every run ending — does not
    # pull ~19 packages including aiohttp into any process that merely records
    # a status.
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={
                "endpoint": device.endpoint,
                "keys": {"p256dh": device.p256dh, "auth": device.auth},
            },
            data=payload,
            vapid_private_key=vapid_private_key(),
            vapid_claims={"sub": vapid_subject()},
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except WebPushException as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status in GONE_STATUSES:
            # The browser threw the subscription away and only the push service
            # knows. Disabled rather than deleted, so the device still appears
            # in the list with a reason beside it.
            device.enabled = False
            device.last_error = f"The push service says this subscription is gone ({status})."
            logger.info("Subscription %s is gone; disabled.", device.id)
        else:
            device.last_error = str(error)[:500]
            logger.warning("Push to %s failed: %s", device.id, error)
        return False
    except Exception as error:
        # A push must never be able to fail a run. Anything the library throws
        # that is not its own exception type lands here.
        device.last_error = str(error)[:500]
        logger.warning("Push to %s could not be sent: %s", device.id, error)
        return False

    device.last_seen_at = datetime.now(UTC)
    device.last_error = None
    return True


def _owner_of(run: Run) -> int | None:
    """Whose run this is. A task run reaches its project through its task; a
    project conversation has one directly."""
    if run.task is not None and run.task.project is not None:
        return run.task.project.user_id
    if run.project is not None:
        return run.project.user_id
    return None


def about_run(run: Run) -> tuple[str, str, str] | None:
    """What to say about a run that just reached `status`, or None for a
    transition nobody needs telling about.

    Split out from the sending so the wording is testable without a push
    service, and so the decision about *which* transitions are worth an
    interruption sits in one readable place.
    """
    task = run.task
    what = task.title if task is not None else "A conversation"

    if run.status is RunStatus.AWAITING_ANSWER:
        return (
            RUN_NEEDS_YOU,
            f"{what} — the agent asked a question",
            (run.outcome_detail or "It stopped and is waiting for an answer."),
        )
    if run.status is RunStatus.AWAITING_REVIEW:
        return (
            RUN_NEEDS_YOU,
            f"{what} — a plan is ready to read",
            (run.outcome_detail or "Nothing happens until you approve it."),
        )
    if run.status is RunStatus.SUCCEEDED:
        return RUN_FINISHED, f"{what} — done", (run.summary or "The run finished.")[:200]
    if run.status is RunStatus.FAILED:
        return RUN_FINISHED, f"{what} — failed", (run.error or "The run failed.")[:200]
    # Cancelled is deliberately silent: somebody pressed the button, so they
    # already know, and being told about it is being told what you just did.
    return None


def about_run_and_notify(db: Session, run: Run) -> int:
    """The trigger. Called from `finish_run`, which every terminal transition
    passes through, and which is the reason this needs no hooks of its own."""
    decided = about_run(run)
    if decided is None:
        return 0
    kind, title, body = decided
    owner = _owner_of(run)
    if owner is None:
        return 0
    return notify(db, owner, kind=kind, title=title, body=body, url=f"/runs/{run.id}")
