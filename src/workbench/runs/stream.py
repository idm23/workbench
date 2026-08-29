"""Streaming a run's events to a browser, resumably.

`run_events` exists so that watching a run is a *query*, not a subscription.
Everything the agent does is committed as it happens, numbered per run, so a
reader that says "I have seen up to 41" can always be told the rest — whether
it disconnected a second ago or a phone slept through the whole thing.

That is the entire design. There is no in-process pub/sub here and there could
not be: the runner is a different process, in a different cgroup, possibly
started before this web process existed. Nothing it does can notify anything in
here. So this polls the table, which sounds crude and is exactly right — the
table is the only thing the two processes share, and it is durable.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.database.db import session_scope
from workbench.database.models import Run, RunEvent, RunStatus

logger = logging.getLogger(__name__)

#: How often to look for new events while a run is going. Fast enough that
#: output feels live, slow enough that a phone left open is not hammering a
#: SQLite file for an hour.
POLL_SECONDS = 1.0

#: Send a comment frame if nothing else has gone out for this long. Proxies and
#: phone radios drop connections that look idle, and a comment is the cheapest
#: thing that proves otherwise.
HEARTBEAT_SECONDS = 15.0

#: Most events read in one go. A run with thousands of tool calls should reach
#: the browser in batches rather than as one query that holds a thread.
BATCH = 200

#: How long to keep sweeping after a run looks finished.
#:
#: `finish_run` commits the row's terminal status and *then* appends the status
#: event, so a reader that stopped the moment it saw a terminal status could
#: miss the last event. Sweeping past the end for a moment makes this reader
#: correct regardless of the writer's commit order — which is the property that
#: survives someone reworking the writer later.
TRAILING_SWEEPS = 3


@dataclass(frozen=True)
class Frame:
    """One server-sent event, already numbered."""

    seq: int
    kind: str
    payload: dict

    def render(self) -> str:
        """The wire format.

        `id:` is the sequence number, which is what the browser sends back as
        `Last-Event-ID` when it reconnects — so replay needs no cookie, no
        session, and no state held here.

        The payload is compact JSON, which cannot contain a raw newline, so it
        never needs splitting across several `data:` lines.
        """
        body = json.dumps(self.payload, separators=(",", ":"))
        return f"id: {self.seq}\nevent: {self.kind}\ndata: {body}\n\n"


def comment(text: str) -> str:
    """A frame that carries nothing, to keep an idle connection open."""
    return f": {text}\n\n"


def fetch_events(db: Session, run_id: int, after_seq: int, limit: int = BATCH) -> list[Frame]:
    """Everything recorded for this run after a sequence number.

    This is both the replay and the tail: the only difference between "catch me
    up on the last hour" and "what happened in the last second" is the number
    passed in.
    """
    rows = db.execute(
        select(RunEvent.seq, RunEvent.kind, RunEvent.payload)
        .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
        .order_by(RunEvent.seq)
        .limit(limit)
    ).all()
    return [Frame(seq=seq, kind=str(kind), payload=payload or {}) for seq, kind, payload in rows]


def run_status(db: Session, run_id: int) -> RunStatus | None:
    return db.scalar(select(Run.status).where(Run.id == run_id))


def _read(run_id: int, after_seq: int) -> tuple[list[Frame], RunStatus | None]:
    """One synchronous look at the database, for `asyncio.to_thread`.

    Events and status are read in one session so they cannot disagree about
    which moment they describe.
    """
    with session_scope() as db:
        return fetch_events(db, run_id, after_seq), run_status(db, run_id)


def parse_last_event_id(raw: str | None) -> int:
    """Where a reconnecting browser got to.

    Anything unparseable means "from the beginning" rather than an error: the
    header is set by the browser but reachable by anyone, and the worst case of
    starting over is a page that repeats itself.
    """
    if not raw:
        return 0
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        logger.warning("Ignoring unparseable Last-Event-ID: %r", raw)
        return 0


async def stream(run_id: int, after_seq: int = 0) -> AsyncIterator[str]:
    """Replay from `after_seq`, then follow the run until it ends.

    The database work goes through `asyncio.to_thread` because this is the
    app's one `async def` route and blocking it would stall every other
    request. That was decided when the schema landed; this is the endpoint it
    was decided for.
    """
    # Tells the browser how long to wait before reconnecting. Without it the
    # default is three seconds, which is a long time to stare at a stalled run.
    yield "retry: 2000\n\n"

    last = after_seq
    idle_for = 0.0
    finishing = 0

    while True:
        try:
            frames, status = await asyncio.to_thread(_read, run_id, last)
        except Exception:
            # A reader failing is not worth taking the request down over, and
            # the browser will reconnect and replay from where it got to.
            logger.exception("Run %s stream failed while reading.", run_id)
            yield comment("read failed")
            return

        if status is None:
            # Deleted mid-stream: the task went away and took its runs with it.
            yield comment("gone")
            return

        for frame in frames:
            last = frame.seq
            yield frame.render()

        if frames:
            idle_for = 0.0
            # A full batch means there is more waiting; do not sleep on it.
            if len(frames) == BATCH:
                continue
        else:
            idle_for += POLL_SECONDS

        if status.is_terminal or status is RunStatus.AWAITING_REVIEW:
            finishing += 1
            if finishing >= TRAILING_SWEEPS:
                # `end` rather than closing silently, so the page can stop its
                # spinner instead of waiting for a reconnect that never helps.
                yield f"event: end\ndata: {json.dumps({'status': status.value})}\n\n"
                return
        else:
            finishing = 0

        if idle_for >= HEARTBEAT_SECONDS:
            idle_for = 0.0
            yield comment("still here")

        await asyncio.sleep(POLL_SECONDS)
