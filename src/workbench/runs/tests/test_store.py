"""Writes against a run and its event log.

The runner and, later, the cancel and reap paths all move a run to a terminal
state. They have to do it the same way, which is why the writes live in one
module and their behaviour is pinned here rather than in any one caller.
"""

from workbench.database.models import RunEvent, RunEventKind, RunOutcome, RunPhase, RunStatus
from workbench.runs.store import (
    append_event,
    create_run,
    finish_run,
    mark_running,
    next_seq,
    record_launch,
    report_outcome,
)


def test_a_new_run_is_queued_with_nothing_started(db, task):
    run = create_run(db, task, RunPhase.PLAN, backend="fake")

    assert run.status is RunStatus.QUEUED
    assert run.handle is None
    assert run.finished_at is None


def test_sequence_numbers_start_at_one_and_increase(db, run):
    first = append_event(db, run.id, RunEventKind.TEXT, {"text": "a"})
    second = append_event(db, run.id, RunEventKind.TEXT, {"text": "b"})

    assert (first.seq, second.seq) == (1, 2)


def test_sequences_are_independent_per_run(db, task, run):
    other = create_run(db, task, RunPhase.PLAN, backend="fake")
    append_event(db, run.id, RunEventKind.TEXT, {"text": "a"})

    assert next_seq(db, other.id) == 1


def test_the_numbering_survives_a_restart(db, run):
    """Computed from the table, not from a counter the process was holding."""
    append_event(db, run.id, RunEventKind.TEXT, {"text": "a"})
    append_event(db, run.id, RunEventKind.TEXT, {"text": "b"})

    assert next_seq(db, run.id) == 3


def test_an_event_is_committed_immediately(db, run):
    """An event still inside a transaction is invisible to the stream reading it."""
    append_event(db, run.id, RunEventKind.TEXT, {"text": "a"})

    assert not db.in_transaction() or db.get(RunEvent, 1) is not None


def test_the_launch_is_recorded_before_anything_runs(db, run):
    """A run executing while nothing knows how to stop it is unreachable."""
    record_launch(db, run, executor="systemd-unit", handle="workbench-run@1.service")

    assert run.executor == "systemd-unit"
    assert run.handle == "workbench-run@1.service"
    assert run.status is RunStatus.QUEUED


def test_starting_a_run_logs_the_transition(db, run):
    mark_running(db, run)

    events = db.query(RunEvent).filter_by(run_id=run.id).all()
    assert [e.kind for e in events] == [RunEventKind.STATUS]
    assert events[0].payload == {"status": "running"}


def test_finishing_records_the_outcome_and_the_time(db, run):
    finish_run(db, run, RunStatus.SUCCEEDED, summary="Did it.")

    assert run.status is RunStatus.SUCCEEDED
    assert run.summary == "Did it."
    assert run.finished_at is not None


def test_finishing_clears_the_handle(db, run):
    """A stale handle invites stopping something that is no longer the run."""
    record_launch(db, run, executor="local-process", handle="4242")
    mark_running(db, run)
    finish_run(db, run, RunStatus.SUCCEEDED)

    assert run.handle is None
    assert run.executor == "local-process"


def test_finishing_leaves_fields_it_was_not_given(db, run):
    finish_run(db, run, RunStatus.FAILED, error="boom")

    assert run.error == "boom"
    assert run.summary is None
    assert run.plan is None


def test_a_partial_failure_still_records_what_it_learned(db, run):
    """The resume token and the usage cannot be reconstructed afterwards."""
    finish_run(
        db,
        run,
        RunStatus.FAILED,
        error="crashed",
        resume_token="session-abc",
        total_cost_usd=0.12,
        num_turns=9,
    )

    assert run.resume_token == "session-abc"
    assert run.total_cost_usd == 0.12
    assert run.num_turns == 9


def test_awaiting_review_is_finished_but_not_terminal(db, task):
    """The run stopped; the task did not. That distinction is the plan phase."""
    run = create_run(db, task, RunPhase.PLAN, backend="fake")
    finish_run(db, run, RunStatus.AWAITING_REVIEW, plan="Here is the plan.")

    assert run.finished_at is not None
    assert run.status.is_terminal is False


def test_reporting_an_outcome_is_visible_before_the_run_finishes(db, run):
    """The point of a live write: it survives even if the process never
    reaches `finish_run` at all — a crash, a killed unit."""
    report_outcome(db, run, RunOutcome.NEEDS_REPLANNING, "hit something unexpected")

    assert run.agent_outcome is RunOutcome.NEEDS_REPLANNING
    assert run.outcome_detail == "hit something unexpected"
    assert run.status is RunStatus.QUEUED
    assert run.finished_at is None


def test_reporting_an_outcome_with_no_detail_leaves_it_unset(db, run):
    report_outcome(db, run, RunOutcome.FINISHED)

    assert run.agent_outcome is RunOutcome.FINISHED
    assert run.outcome_detail is None
