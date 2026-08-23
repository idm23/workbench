"""How close the account is to its rate limits.

Runs bill a subscription, so the resource that actually runs out is a window —
five hours, or seven days — rather than a budget. A number on the page is
worth more here than a cost column: exhausting a window stops not just
Workbench but whatever else uses the same account, and the useful moment to
find out is before starting a run rather than halfway through one.

The readings are recovered from `run_events` rather than kept in a table of
their own. The event log already records every one the backend reported, with a
timestamp, which makes it the honest source — a cached number would have to be
invalidated by exactly the events this reads.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from workbench.database.models import RunEvent, RunEventKind

logger = logging.getLogger(__name__)

#: Human names for the backend's window identifiers. A window with no entry is
#: shown under its raw name rather than hidden — a new one appearing is
#: information, and silently dropping it would be the wrong default.
WINDOW_LABELS = {
    "five_hour": "5-hour",
    "seven_day": "7-day",
    "seven_day_opus": "7-day Opus",
    "seven_day_sonnet": "7-day Sonnet",
    "overage": "Overage",
}

#: Where a bar changes colour. Below this the number is background information;
#: above it, it is a reason to think before starting another run.
WARNING_AT = 0.75

#: How far back to look. A reading older than this describes a window that has
#: almost certainly since reset, and a stale bar is worse than none.
MAX_AGE_HOURS = 24

#: Bound on the rows scanned. Rate-limit notices are rare next to tool calls,
#: so this is the cheap half of "recent events" rather than a real limit on how
#: many readings exist.
SCAN_LIMIT = 500


def _timestamp(value: int | float | None) -> datetime | None:
    """A backend's epoch timestamp as a datetime.

    The SDK documents this as Unix seconds, and the guard below is here anyway
    because the same vendor's credential file next door stores milliseconds. A
    reset time rendered a thousand-fold wrong would read as a bug in Workbench,
    and the check costs one comparison.
    """
    if value is None:
        return None
    seconds = value / 1000 if value > 1e11 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except OverflowError, OSError, ValueError:
        logger.warning("Uninterpretable rate limit reset timestamp: %r", value)
        return None


@dataclass(frozen=True)
class RateLimitReading:
    """The most recent thing the backend said about one window."""

    window: str
    status: str
    utilization: float | None
    resets_at: datetime | None
    observed_at: datetime

    @property
    def label(self) -> str:
        return WINDOW_LABELS.get(self.window, self.window.replace("_", " "))

    @property
    def percent(self) -> int | None:
        """Utilisation as a whole number, clamped into range.

        Clamped because this drives a bar's width, and a backend reporting
        1.02 should produce a full bar rather than one overflowing its box.
        """
        if self.utilization is None:
            return None
        return round(max(0.0, min(1.0, self.utilization)) * 100)

    @property
    def status_label(self) -> str:
        """The backend's status in words, for when there is no percentage.

        `utilization` is optional in the protocol and genuinely absent in real
        readings — the one recorded sample on this machine carries a status, a
        window, and a reset time and no number at all. A panel that showed
        `allowed` in that case would be worse than useless, so the status
        carries the meaning whenever the number cannot.
        """
        return {
            "allowed": "within limits",
            "allowed_warning": "close to the limit",
            "rejected": "limit reached",
        }.get(self.status, self.status.replace("_", " "))

    @property
    def level(self) -> str:
        """`ok`, `warning`, or `exhausted` — the CSS class and the meaning.

        Driven by the backend's own status first, and by utilisation only as a
        fallback: `rejected` means the limit was actually hit, which is a
        stronger statement than any threshold of ours.
        """
        if self.status == "rejected":
            return "exhausted"
        if self.status == "allowed_warning":
            return "warning"
        if self.utilization is not None and self.utilization >= WARNING_AT:
            return "warning"
        return "ok"

    @property
    def resets_in(self) -> str | None:
        """Time until the window resets, in the roughest useful unit."""
        if self.resets_at is None:
            return None
        remaining = self.resets_at - datetime.now(UTC)
        minutes = int(remaining.total_seconds() // 60)
        if minutes <= 0:
            return "any moment"
        if minutes < 60:
            return f"{minutes}m"
        hours, minutes = divmod(minutes, 60)
        if hours < 24:
            return f"{hours}h {minutes}m" if minutes else f"{hours}h"
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h" if hours else f"{days}d"

    @property
    def age(self) -> str:
        """How long ago the backend said this.

        Shown because these readings only refresh when something talks to the
        backend, so the panel is a snapshot of the last time that happened
        rather than a live gauge. Without the age, a number from this morning
        looks exactly like one from a minute ago.
        """
        minutes = int((datetime.now(UTC) - self.observed_at).total_seconds() // 60)
        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{minutes}m ago"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h ago" if hours < 24 else f"{hours // 24}d ago"

    @property
    def is_stale(self) -> bool:
        age = datetime.now(UTC) - self.observed_at
        return age.total_seconds() > MAX_AGE_HOURS * 3600


def _reading(payload: dict, observed_at: datetime) -> RateLimitReading | None:
    limit = payload.get("rate_limit")
    if not isinstance(limit, dict) or not limit.get("type"):
        return None
    return RateLimitReading(
        window=str(limit["type"]),
        status=str(limit.get("status", "allowed")),
        utilization=limit.get("utilization"),
        resets_at=_timestamp(limit.get("resets_at")),
        observed_at=observed_at,
    )


def latest_readings(db: Session) -> list[RateLimitReading]:
    """The newest reading for each window, tightest first.

    One row per window rather than a history: the question this answers is
    "how much is left", and only the last answer bears on it. Ordered by how
    close each window is to full so the one about to matter is read first.
    """
    rows = db.execute(
        select(RunEvent.payload, RunEvent.created_at)
        .where(
            RunEvent.kind == RunEventKind.NOTICE,
            # Filtered in SQL so this stays a narrow scan of the largest table
            # in the schema rather than deserialising every notice ever logged.
            func.json_extract(RunEvent.payload, "$.rate_limit.type").is_not(None),
        )
        .order_by(desc(RunEvent.id))
        .limit(SCAN_LIMIT)
    ).all()

    newest: dict[str, RateLimitReading] = {}
    for payload, created_at in rows:
        observed_at = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        reading = _reading(payload, observed_at)
        # Rows arrive newest first, so the first sighting of a window wins.
        if reading is not None and reading.window not in newest and not reading.is_stale:
            newest[reading.window] = reading

    return sorted(newest.values(), key=lambda r: (-(r.utilization or 0.0), r.label))
