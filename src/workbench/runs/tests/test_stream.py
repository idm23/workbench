"""Streaming a run's events, resumably.

The property worth defending is that a reader can always be caught up. A phone
that sleeps through half a run, a proxy that drops an idle connection, a
browser tab restored an hour later — each reconnects saying how far it got, and
must be told the rest exactly once.

The stream is driven directly rather than through HTTP here. What is being
tested is the resumption arithmetic and the decision about when a run is over,
neither of which needs a socket.
"""

import asyncio
import json

import pytest

from workbench.database.models import RunEventKind, RunPhase, RunStatus
from workbench.runs import stream as stream_module
from workbench.runs.store import append_event, create_run, finish_run, mark_running
from workbench.runs.stream import (
    Frame,
    fetch_events,
    parse_last_event_id,
    stream,
)


@pytest.fixture(autouse=True)
def brisk(monkeypatch):
    """Real timing, minus the waiting."""
    monkeypatch.setattr(stream_module, "POLL_SECONDS", 0.001)
    monkeypatch.setattr(stream_module, "HEARTBEAT_SECONDS", 0.002)


def drain(run_id: int, after: int = 0, limit: int = 500) -> list[str]:
    """Everything the stream yields until it ends."""

    async def collect() -> list[str]:
        out: list[str] = []
        async for chunk in stream(run_id, after):
            out.append(chunk)
            if len(out) >= limit:
                break
        return out

    return asyncio.run(collect())


def frames(chunks: list[str]) -> list[tuple[int, str]]:
    """(seq, kind) for the real events, ignoring comments and control frames."""
    found = []
    for chunk in chunks:
        if not chunk.startswith("id: "):
            continue
        lines = chunk.splitlines()
        found.append((int(lines[0][4:]), lines[1][len("event: ") :]))
    return found


def a_run(db, task, *, status=RunStatus.SUCCEEDED, events=3):
    run = create_run(db, task, RunPhase.EXECUTE, backend="fake")
    mark_running(db, run)
    for n in range(events):
        append_event(db, run.id, RunEventKind.TEXT, {"text": f"line {n}"})
    if status is not RunStatus.RUNNING:
        finish_run(db, run, status, summary="done")
    return run


# --- The wire format -------------------------------------------------------


def test_a_frame_carries_its_sequence_as_the_event_id():
    """That id is what comes back as Last-Event-ID, so replay needs no state."""
    rendered = Frame(seq=41, kind="text", payload={"text": "hello"}).render()

    assert rendered.startswith("id: 41\nevent: text\ndata: ")
    assert rendered.endswith("\n\n")


def test_a_payload_never_spans_several_data_lines():
    """A raw newline in the body would end the frame early."""
    rendered = Frame(seq=1, kind="text", payload={"text": "two\nlines"}).render()

    assert rendered.count("data: ") == 1
    assert json.loads(rendered.split("data: ", 1)[1].strip())["text"] == "two\nlines"


# --- Resumption ------------------------------------------------------------


def test_a_reader_starting_cold_gets_everything(db, task):
    run = a_run(db, task, events=3)

    assert [seq for seq, _ in frames(drain(run.id))] == [1, 2, 3, 4, 5]


def test_a_reader_resuming_gets_only_what_it_missed(db, task):
    """The whole point: no duplicates, no gaps."""
    run = a_run(db, task, events=3)

    seqs = [seq for seq, _ in frames(drain(run.id, after=3))]

    assert seqs == [4, 5]


def test_resuming_past_the_end_yields_nothing_new(db, task):
    run = a_run(db, task, events=3)

    assert frames(drain(run.id, after=99)) == []


def test_the_two_halves_together_are_the_whole_log(db, task):
    """A reconnect must not lose the event that arrived during the gap."""
    run = a_run(db, task, events=4)
    first = frames(drain(run.id, after=0))[:2]
    resumed = frames(drain(run.id, after=first[-1][0]))

    assert [seq for seq, _ in first + resumed] == [1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize(
    ("header", "expected"),
    [("41", 41), ("  7 ", 7), (None, 0), ("", 0), ("nonsense", 0), ("-5", 0)],
)
def test_last_event_id_is_read_forgivingly(header, expected):
    """Anything unparseable means "start over", which is safe rather than fatal."""
    assert parse_last_event_id(header) == expected


# --- Knowing when to stop --------------------------------------------------


def test_a_finished_run_ends_the_stream(db, task):
    run = a_run(db, task, status=RunStatus.SUCCEEDED)

    chunks = drain(run.id)

    assert any(chunk.startswith("event: end") for chunk in chunks)


def test_the_final_event_is_never_missed(db, task):
    """`finish_run` commits the status *before* appending its status event.

    A reader that stopped the moment it saw a terminal status would miss that
    last event. Sweeping past the end is what makes this reader correct
    whatever order the writer happens to use.
    """
    run = a_run(db, task, status=RunStatus.SUCCEEDED, events=1)

    kinds = [kind for _, kind in frames(drain(run.id))]

    assert kinds[-1] == RunEventKind.STATUS.value


def test_a_plan_awaiting_review_also_ends(db, task):
    """It is not terminal, but nothing further will arrive until a person acts."""
    run = create_run(db, task, RunPhase.PLAN, backend="fake")
    mark_running(db, run)
    finish_run(db, run, RunStatus.AWAITING_REVIEW, plan="a plan")

    assert any(chunk.startswith("event: end") for chunk in drain(run.id))


def test_the_end_frame_says_how_it_ended(db, task):
    run = a_run(db, task, status=RunStatus.FAILED)

    end = next(c for c in drain(run.id) if c.startswith("event: end"))

    assert json.loads(end.split("data: ", 1)[1].strip())["status"] == "failed"


def test_a_run_that_vanished_closes_rather_than_hanging(db, task):
    """The task was deleted, taking its runs with it."""
    chunks = drain(9999)

    assert any("gone" in chunk for chunk in chunks)


def test_a_running_run_is_followed_rather_than_ended(db, task):
    """It must keep listening, so this is bounded by the drain limit not by `end`."""
    run = a_run(db, task, status=RunStatus.RUNNING, events=1)

    chunks = drain(run.id, limit=6)

    assert not any(chunk.startswith("event: end") for chunk in chunks)


def test_an_idle_stream_says_it_is_still_there(db, task):
    """Proxies and phone radios drop connections that look dead."""
    run = a_run(db, task, status=RunStatus.RUNNING, events=0)

    chunks = drain(run.id, limit=8)

    assert any(chunk.startswith(": ") for chunk in chunks)


def test_the_browser_is_told_how_soon_to_reconnect(db, task):
    run = a_run(db, task)

    assert drain(run.id)[0].startswith("retry: ")


# --- Paging ----------------------------------------------------------------


def test_a_long_log_is_read_in_batches(db, task, monkeypatch):
    """A run with thousands of tool calls should not be one query."""
    monkeypatch.setattr(stream_module, "BATCH", 2)
    run = a_run(db, task, events=5)

    assert [seq for seq, _ in frames(drain(run.id))] == [1, 2, 3, 4, 5, 6, 7]


def test_fetch_respects_its_limit(db, task):
    run = a_run(db, task, events=5)

    assert len(fetch_events(db, run.id, after_seq=0, limit=3)) == 3


def test_a_read_failure_closes_the_stream_rather_than_the_request(db, task, monkeypatch):
    """The browser reconnects and replays; taking the app down helps nobody."""
    run = a_run(db, task)

    def explode(*_):
        raise RuntimeError("database went away")

    monkeypatch.setattr(stream_module, "_read", explode)

    chunks = drain(run.id)

    assert any("read failed" in chunk for chunk in chunks)
