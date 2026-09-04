"""The detached runner.

What matters here is not that a happy run works, but that every way a run can
end still ends in a recorded outcome. A run left in `running` with a dead pid
holds a concurrency slot forever and explains nothing to whoever finds it, so
each failure path gets its own test.
"""

import asyncio
import os
import signal

import pytest

from workbench.agents.protocol import (
    AgentEvent,
    AgentFailed,
    AgentFinished,
    AgentRequest,
    AgentUnavailable,
    SubtaskProposal,
)
from workbench.agents.registry import UnknownBackend
from workbench.agents.tests.fake import FakeBackend
from workbench.database.models import (
    RunEvent,
    RunEventKind,
    RunOutcome,
    RunPhase,
    RunStatus,
    TaskStatus,
)
from workbench.runs import runner as runner_module
from workbench.runs.runner import (
    Interrupted,
    NotPrepared,
    execute,
    main,
    prepare,
    resume_token_for,
    resume_token_for_project,
)
from workbench.runs.store import create_conversation, create_run, finish_run


@pytest.fixture
def backend(monkeypatch):
    """Install a scripted backend in place of whatever the registry would give."""
    fake = FakeBackend(
        events=[
            AgentEvent(RunEventKind.TEXT, {"text": "Looking at the code."}),
            AgentEvent(RunEventKind.TOOL_USE, {"name": "Bash", "input": {"command": "ls"}}),
        ],
        outcome=AgentFinished(
            text="Added the tests.",
            resume_token="session-abc",
            model="claude-x",
            total_cost_usd=0.31,
            num_turns=7,
        ),
    )
    monkeypatch.setattr(runner_module, "get_backend", lambda _name: fake)
    return fake


def events_for(db, run):
    return db.query(RunEvent).filter_by(run_id=run.id).order_by(RunEvent.seq).all()


# --- The happy path --------------------------------------------------------


def test_a_finished_execute_run_succeeds_with_its_summary(db, run, checkout, backend):
    execute(db, run)

    assert run.status is RunStatus.SUCCEEDED
    assert run.summary == "Added the tests."


def test_usage_and_the_resume_token_are_recorded(db, run, checkout, backend):
    execute(db, run)

    assert run.resume_token == "session-abc"
    assert run.model == "claude-x"
    assert run.total_cost_usd == 0.31
    assert run.num_turns == 7


def test_every_agent_event_reaches_the_log_in_order(db, run, checkout, backend):
    execute(db, run)

    kinds = [e.kind for e in events_for(db, run)]
    assert RunEventKind.TEXT in kinds
    assert RunEventKind.TOOL_USE in kinds
    assert kinds.index(RunEventKind.TEXT) < kinds.index(RunEventKind.TOOL_USE)


def test_the_log_opens_and_closes_with_a_status(db, run, checkout, backend):
    """So a reader can tell a run that never started from one still going."""
    execute(db, run)

    events = events_for(db, run)
    assert events[0].kind is RunEventKind.STATUS
    assert events[0].payload == {"status": "running"}
    assert events[-1].payload == {"status": "succeeded"}


def test_sequence_numbers_are_dense_and_ordered(db, run, checkout, backend):
    """Replay from Last-Event-ID depends on this, so it is worth pinning."""
    execute(db, run)

    seqs = [e.seq for e in events_for(db, run)]
    assert seqs == list(range(1, len(seqs) + 1))


def test_the_worktree_is_recorded_on_the_task(db, run, checkout, backend):
    """On the task, because the execute run after a plan run reuses it."""
    execute(db, run)

    assert run.task.branch == "workbench/task-1-add-route-tests"
    assert run.task.worktree_path is not None


def test_the_agent_is_pointed_at_that_worktree(db, run, checkout, backend):
    execute(db, run)

    assert str(backend.requests[0].worktree) == run.task.worktree_path


def test_the_handle_is_cleared_once_the_run_is_over(db, run, checkout, backend):
    execute(db, run)

    assert run.handle is None
    statuses = [e for e in events_for(db, run) if e.kind is RunEventKind.STATUS]
    assert statuses[0].payload["status"] == "running"


# --- Agent-reported outcomes -------------------------------------------------


def test_an_explicit_finished_outcome_marks_the_task_done(db, run, checkout, backend):
    run.agent_outcome = RunOutcome.FINISHED
    db.commit()

    execute(db, run)

    assert run.status is RunStatus.SUCCEEDED
    assert run.task.status is TaskStatus.DONE


def test_no_reported_outcome_leaves_the_tasks_status_untouched(db, run, checkout, backend):
    """A correctly-behaving agent always reports; this is the degenerate path."""
    execute(db, run)

    assert run.status is RunStatus.SUCCEEDED
    assert run.task.status is TaskStatus.OPEN
    notices = [e.payload["text"] for e in events_for(db, run) if e.kind is RunEventKind.NOTICE]
    assert any("did not report an outcome" in text for text in notices)


def _report_from_another_process(run_id: int, outcome, detail: str | None = None) -> None:
    """Report an outcome the way the outcome API actually does: from a
    different session, as the web process, while the runner holds its own."""
    from workbench.database.db import get_session_factory
    from workbench.database.models import Run
    from workbench.runs.store import report_outcome

    with get_session_factory()() as elsewhere:
        elsewhere_run = elsewhere.get(Run, run_id)
        assert elsewhere_run is not None
        report_outcome(elsewhere, elsewhere_run, outcome, detail)


def test_an_outcome_reported_by_the_web_process_is_not_missed(db, run, checkout, backend):
    """The gap every test around this one steps over.

    They all set `run.agent_outcome` on the runner's own session, where it is
    trivially visible. In production nothing does that: the agent reports
    through the HTTP API, so the write lands in the web process's session,
    and the factory sets `expire_on_commit=False` — so the runner's cached
    Run keeps the None it loaded at startup and every outcome is missed.

    Observed on the real server: a run whose agent reported `finished`, whose
    page showed `Outcome: finished`, and whose task was left open under a
    notice saying no outcome was reported.
    """
    _report_from_another_process(run.id, RunOutcome.FINISHED)

    execute(db, run)

    assert run.task.status is TaskStatus.DONE
    notices = [e.payload["text"] for e in events_for(db, run) if e.kind is RunEventKind.NOTICE]
    assert not any("did not report an outcome" in text for text in notices)


def test_a_failure_reported_by_the_web_process_still_fails_the_run(db, run, checkout, backend):
    """The same staleness, on the branch where it costs most.

    A missed `failed` did not merely leave a task open — it recorded the run
    as succeeded, because the guards fell through to the default.
    """
    _report_from_another_process(run.id, RunOutcome.FAILED, "Tests fail and I could not find why.")

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert run.task.status is TaskStatus.BLOCKED
    assert run.error == "Tests fail and I could not find why."


# --- Publishing the work -----------------------------------------------------
#
# `execute_prompt` tells the agent not to push and not to open a pull request,
# because Workbench does both. Until this existed that was a promise nothing
# kept: the agent obeyed, committed, and the work sat in a worktree.


def _finished(db, run):
    """A run the agent reported as finished, the way the outcome API does."""
    _report_from_another_process(run.id, RunOutcome.FINISHED)


def _publishes(
    monkeypatch,
    *,
    commits: bool | object = True,
    push=None,
    opened=None,
    token: str | None = "pat",
):
    """Stand in for the three collaborators `_publish` orchestrates."""
    from workbench.git.github import PullRequestOpened
    from workbench.git.worktrees import GitOk

    calls: dict = {}

    def push_branch(worktree, branch):
        calls["pushed"] = branch
        return push if push is not None else GitOk("")

    def open_pull_request(ref, **kwargs):
        calls["pr"] = kwargs
        return opened if opened is not None else PullRequestOpened(url="https://gh/pr/1")

    monkeypatch.setattr(runner_module, "has_commits", lambda *_: commits)
    monkeypatch.setattr(runner_module, "push_branch", push_branch)
    monkeypatch.setattr(runner_module, "open_pull_request", open_pull_request)
    monkeypatch.setattr(runner_module, "github_token", lambda: token)
    return calls


def test_a_finished_run_pushes_and_records_the_pull_request(
    db, run, checkout, backend, monkeypatch
):
    calls = _publishes(monkeypatch)
    _finished(db, run)

    execute(db, run)

    assert calls["pushed"] == run.task.branch
    assert run.pr_url == "https://gh/pr/1"


def test_the_pull_request_targets_what_the_worktree_was_cut_from(
    db, run, checkout, backend, monkeypatch
):
    """Not the repository's default branch. This is what sends work into
    staging on a project that promotes through it."""
    import subprocess

    calls = _publishes(monkeypatch)
    subprocess.run(("git", "branch", "staging"), cwd=checkout, check=True, capture_output=True)
    run.task.origin_ref = "staging"
    db.commit()
    _finished(db, run)

    execute(db, run)

    assert calls["pr"]["base"] == "staging"
    assert calls["pr"]["head"] == run.task.branch


def test_a_run_with_no_commits_is_not_pushed(db, run, checkout, backend, monkeypatch):
    """An agent that correctly concluded nothing needed changing. An empty
    pull request would be the worst possible answer to that."""
    calls = _publishes(monkeypatch, commits=False)
    _finished(db, run)

    execute(db, run)

    assert "pushed" not in calls
    assert run.pr_url is None
    assert _notices(db, run, "nothing was pushed")


def test_a_check_that_could_not_run_is_not_reported_as_nothing_to_push(
    db, run, checkout, backend, monkeypatch
):
    """What actually happened on the server, and why it was invisible.

    The base ref would not resolve, `has_commits` turned that into False, and
    a run that had made a commit announced that it had not. Nobody had a
    reason to look, and the work sat unpushed a second time.
    """
    from workbench.git.worktrees import GitFailed

    calls = _publishes(
        monkeypatch,
        commits=GitFailed("git rev-list failed (exit 128).", stderr="unknown revision"),
    )
    _finished(db, run)

    execute(db, run)

    assert "pushed" not in calls
    assert _notices(db, run, "Could not tell")
    assert not _notices(db, run, "No commits on this branch")


def test_a_failed_push_says_so_without_failing_the_run(db, run, checkout, backend, monkeypatch):
    """The work is committed by this point. Recording the run as a failure
    would make those commits look suspect over an SSH key."""
    from workbench.git.worktrees import GitFailed

    _publishes(monkeypatch, push=GitFailed("git push failed (exit 128).", stderr="denied"))
    _finished(db, run)

    execute(db, run)

    assert run.status is RunStatus.SUCCEEDED
    assert run.task.status is TaskStatus.DONE
    assert run.pr_url is None
    assert _notices(db, run, "could not be pushed")
    # The doctor is where the deploy key is diagnosed, so it is named here.
    assert _notices(db, run, "workbench.doctor")


def test_without_a_token_the_branch_is_still_pushed(db, run, checkout, backend, monkeypatch):
    """Pushing uses the deploy key; opening a pull request needs the API. Only
    the second one is missing, so only the second one is given up on."""
    calls = _publishes(monkeypatch, token=None)
    _finished(db, run)

    execute(db, run)

    assert calls["pushed"] == run.task.branch
    assert run.pr_url is None
    assert _notices(db, run, "WORKBENCH_GITHUB_TOKEN")


def test_an_unreported_run_is_not_published(db, run, checkout, backend, monkeypatch):
    """The same standard that closes a task opens a pull request. A run that
    never said how it went leaves its commits for a person to look at."""
    calls = _publishes(monkeypatch)

    execute(db, run)

    assert "pushed" not in calls
    assert run.pr_url is None


def _notices(db, run, needle: str) -> bool:
    return any(
        needle in e.payload.get("text", "")
        for e in events_for(db, run)
        if e.kind is RunEventKind.NOTICE
    )


def test_a_reported_finished_outcome_is_distrusted_if_the_run_was_cut_short(
    db, run, checkout, monkeypatch
):
    """Hitting the turn limit still yields AgentFinished, so self-reported
    success alone is not enough to trust — see `stopped_early`."""
    run.agent_outcome = RunOutcome.FINISHED
    db.commit()
    monkeypatch.setattr(
        runner_module,
        "get_backend",
        lambda _n: FakeBackend(outcome=AgentFinished(text="done", stopped_early=True)),
    )

    execute(db, run)

    assert run.status is RunStatus.SUCCEEDED
    assert run.task.status is TaskStatus.OPEN
    notices = [e.payload["text"] for e in events_for(db, run) if e.kind is RunEventKind.NOTICE]
    assert any("cut off" in text for text in notices)


def test_needs_replanning_pauses_for_a_person_and_blocks_the_task(db, run, checkout, backend):
    run.agent_outcome = RunOutcome.NEEDS_REPLANNING
    run.outcome_detail = "The instructions assumed a config file that doesn't exist."
    db.commit()

    execute(db, run)

    assert run.status is RunStatus.AWAITING_REVIEW
    assert run.task.status is TaskStatus.BLOCKED
    assert run.error is None


def test_a_reported_failure_fails_the_run_and_blocks_the_task(db, run, checkout, backend):
    run.agent_outcome = RunOutcome.FAILED
    run.outcome_detail = "Tests fail and I could not find why."
    db.commit()

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert run.task.status is TaskStatus.BLOCKED
    assert run.error == "Tests fail and I could not find why."


def test_a_plans_proposed_subtasks_are_stored(db, task, checkout, monkeypatch):
    fake = FakeBackend(
        outcome=AgentFinished(
            text="Plan text",
            proposed_subtasks=[
                SubtaskProposal(title="Do part A", body="details", ready_to_execute=True),
                SubtaskProposal(title="Investigate B", body="details"),
            ],
        )
    )
    monkeypatch.setattr(runner_module, "get_backend", lambda _n: fake)
    run = create_run(db, task, RunPhase.PLAN, backend="fake")

    execute(db, run)

    assert run.proposed_subtasks == {
        "subtasks": [
            {"title": "Do part A", "body": "details", "ready_to_execute": True},
            {"title": "Investigate B", "body": "details", "ready_to_execute": False},
        ]
    }


def test_a_plan_with_no_subtasks_stores_none(db, task, checkout, backend):
    run = create_run(db, task, RunPhase.PLAN, backend="fake")

    execute(db, run)

    assert run.proposed_subtasks is None


# --- The plan phase --------------------------------------------------------


def test_a_plan_run_stops_for_a_person(db, task, checkout, backend):
    run = create_run(db, task, RunPhase.PLAN, backend="fake")

    execute(db, run)

    assert run.status is RunStatus.AWAITING_REVIEW
    assert run.plan == "Added the tests."
    assert run.summary is None


def test_the_plan_phase_asks_the_agent_not_to_change_anything(db, task, checkout, backend):
    run = create_run(db, task, RunPhase.PLAN, backend="fake")

    execute(db, run)

    assert "Do not make any changes yet" in backend.requests[0].prompt.replace("\n", " ")


# --- Resuming --------------------------------------------------------------


def test_an_execute_run_resumes_the_plan_that_preceded_it(db, task, checkout, backend):
    plan = create_run(db, task, RunPhase.PLAN, backend="fake")
    finish_run(db, plan, RunStatus.AWAITING_REVIEW, plan="p", resume_token="session-from-plan")
    execute_run = create_run(db, task, RunPhase.EXECUTE, backend="fake")

    execute(db, execute_run)

    assert backend.requests[0].resume_token == "session-from-plan"


def test_a_token_from_another_backend_is_never_reused(db, task):
    """It is opaque, and means nothing to the backend that did not issue it."""
    other = create_run(db, task, RunPhase.PLAN, backend="something-else")
    finish_run(db, other, RunStatus.AWAITING_REVIEW, resume_token="not-ours")

    assert resume_token_for(db, task, "fake") is None


def test_the_most_recent_token_wins(db, task):
    first = create_run(db, task, RunPhase.PLAN, backend="fake")
    finish_run(db, first, RunStatus.FAILED, resume_token="older")
    second = create_run(db, task, RunPhase.PLAN, backend="fake")
    finish_run(db, second, RunStatus.AWAITING_REVIEW, resume_token="newer")

    assert resume_token_for(db, task, "fake") == "newer"


def test_a_first_run_starts_cold(db, task, checkout, backend):
    run = create_run(db, task, RunPhase.PLAN, backend="fake")

    execute(db, run)

    assert backend.requests[0].resume_token is None


# --- Every way it can go wrong ---------------------------------------------


def test_an_uncloned_project_fails_before_anything_is_attempted(db, run, backend):
    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert "cloned" in (run.error or "")
    assert backend.requests == []


def test_an_unknown_backend_name_fails_with_the_names_that_exist(db, run, checkout, monkeypatch):
    monkeypatch.setattr(
        runner_module, "get_backend", lambda name: UnknownBackend(name, ("claude",))
    )

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert "claude" in (run.error or "")


def test_a_failing_setup_command_stops_the_run(db, run, checkout, backend):
    run.task.project.setup_command = "exit 3"
    db.commit()

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert "Setup command failed" in (run.error or "")
    assert backend.requests == []


def test_an_agent_failure_keeps_the_usage_it_reported(db, run, checkout, monkeypatch):
    monkeypatch.setattr(
        runner_module,
        "get_backend",
        lambda _n: FakeBackend(
            outcome=AgentFailed("ran out of turns", resume_token="s1", total_cost_usd=0.9)
        ),
    )

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert run.error == "ran out of turns"
    assert run.total_cost_usd == 0.9
    assert run.resume_token == "s1"


def test_an_unavailable_agent_is_a_failure_with_no_diffstat(db, run, checkout, monkeypatch):
    """Nothing ran, so there is nothing to diff and nothing to summarise."""
    monkeypatch.setattr(
        runner_module, "get_backend", lambda _n: FakeBackend(outcome=AgentUnavailable("no CLI"))
    )

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert run.error == "no CLI"
    assert run.diffstat is None


def test_a_backend_that_yields_no_outcome_still_ends_the_run(db, run, checkout, monkeypatch):
    class Silent:
        name = "silent"

        async def run(self, request: AgentRequest):
            yield AgentEvent(RunEventKind.TEXT, {"text": "hi"})

    monkeypatch.setattr(runner_module, "get_backend", lambda _n: Silent())

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert "no outcome" in (run.error or "")


def test_a_backend_that_raises_does_not_leave_the_run_running(db, run, checkout, monkeypatch):
    """The protocol says it should not. The runner cannot rely on that."""

    class Exploding:
        name = "exploding"

        async def run(self, request: AgentRequest):
            raise RuntimeError("unexpected")
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(runner_module, "get_backend", lambda _n: Exploding())

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert "crashed" in (run.error or "")
    assert run.finished_at is not None


def test_a_run_is_never_left_running_whatever_happens(db, task, checkout, monkeypatch):
    """The property the individual cases above are each an instance of."""
    outcomes = [
        AgentFinished(text="ok"),
        AgentFailed("nope"),
        AgentUnavailable("gone"),
    ]
    for outcome in outcomes:
        monkeypatch.setattr(
            runner_module, "get_backend", lambda _n, o=outcome: FakeBackend(outcome=o)
        )
        run = create_run(db, task, RunPhase.EXECUTE, backend="fake")

        execute(db, run)

        assert run.status is not RunStatus.RUNNING
        assert run.handle is None
        assert run.finished_at is not None


# --- Interruption ----------------------------------------------------------


def test_a_signalled_run_is_cancelled_rather_than_lost(db, run, checkout, monkeypatch):
    """A deploy restarting the service is the ordinary cause of this."""

    class Slow:
        name = "slow"

        async def run(self, request: AgentRequest):
            yield AgentEvent(RunEventKind.TEXT, {"text": "starting"})
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(5)
            yield AgentFinished(text="never reached")

    monkeypatch.setattr(runner_module, "get_backend", lambda _n: Slow())

    execute(db, run)

    assert run.status is RunStatus.CANCELLED
    assert "SIGTERM" in (run.error or "")


def test_work_done_before_the_signal_is_still_in_the_log(db, run, checkout, monkeypatch):
    class Slow:
        name = "slow"

        async def run(self, request: AgentRequest):
            yield AgentEvent(RunEventKind.TEXT, {"text": "starting"})
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(5)
            yield AgentFinished(text="never reached")

    monkeypatch.setattr(runner_module, "get_backend", lambda _n: Slow())

    execute(db, run)

    texts = [e.payload.get("text") for e in events_for(db, run)]
    assert "starting" in texts


def test_the_default_signal_disposition_is_restored(db, run, checkout, backend):
    """A handler left installed would outlive the run inside a test session."""
    before = signal.getsignal(signal.SIGTERM)

    execute(db, run)

    assert signal.getsignal(signal.SIGTERM) == before


# --- prepare() in isolation ------------------------------------------------


def test_prepare_reports_the_project_that_is_missing(db, run):
    result = prepare(db, run)

    assert isinstance(result, NotPrepared)
    assert "idm23/workbench" in result.message


def test_prepare_notes_each_slow_step_in_the_log(db, run, checkout, backend):
    """A first clone or a setup command is slow enough to look like a hang."""
    prepare(db, run)

    notices = [e.payload["text"] for e in events_for(db, run) if e.kind is RunEventKind.NOTICE]
    assert any("worktree" in text for text in notices)


# --- Choosing what to branch from -------------------------------------------


def test_prepare_rejects_an_origin_that_no_longer_resolves(db, run, checkout):
    """Defense in depth: the web route already validates this at request time."""
    run.task.origin_ref = "task:9999"
    db.commit()

    result = prepare(db, run)

    assert isinstance(result, NotPrepared)
    assert "not a valid origin" in result.message


def test_prepare_branches_from_the_chosen_origin(db, run, checkout):
    run.task.origin_ref = "staging"
    db.commit()

    prepare(db, run)

    notices = [e.payload["text"] for e in events_for(db, run) if e.kind is RunEventKind.NOTICE]
    assert any("from staging" in text for text in notices)


def test_prepare_fetches_before_creating_a_new_worktree(db, run, checkout, monkeypatch):
    """The fix for the actual trap: a clone that is never fetched again."""
    real_fetch = runner_module.fetch_checkout
    calls = []
    monkeypatch.setattr(
        runner_module,
        "fetch_checkout",
        lambda repo: calls.append(repo) or real_fetch(repo),
    )

    prepare(db, run)

    assert calls == [checkout]


def test_prepare_does_not_refetch_once_a_worktree_already_exists(
    db, run, checkout, backend, monkeypatch
):
    """Re-fetching a large repository on every execute run would cost time
    for no benefit — the branch is already fixed by then."""
    execute(db, run)
    assert run.task.worktree_path is not None

    calls = []
    monkeypatch.setattr(runner_module, "fetch_checkout", lambda repo: calls.append(repo))
    second = create_run(db, run.task, RunPhase.EXECUTE, backend="fake")

    prepare(db, second)

    assert calls == []


# --- Typing into a run while it goes ----------------------------------------


def test_watch_for_input_delivers_a_message_inserted_after_it_starts(db, run, monkeypatch):
    import contextlib

    from workbench.runs.store import append_input

    monkeypatch.setattr(runner_module, "INPUT_POLL_SECONDS", 0.01)

    async def scenario():
        activity = runner_module._Activity()
        watcher = runner_module._watch_for_input(db, run.id, activity, idle_seconds=1)
        received = []

        async def consume():
            async for body in watcher:
                received.append(body)

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.05)
        append_input(db, run.id, "actually, also check the tests")
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return received

    assert asyncio.run(scenario()) == ["actually, also check the tests"]


def test_watch_for_input_touches_activity_when_something_arrives(db, run, monkeypatch):
    from workbench.runs.store import append_input

    monkeypatch.setattr(runner_module, "INPUT_POLL_SECONDS", 0.01)

    async def scenario():
        activity = runner_module._Activity()
        stale = activity.last
        append_input(db, run.id, "hello")
        watcher = runner_module._watch_for_input(db, run.id, activity, idle_seconds=1)
        body = await watcher.__anext__()
        return body, stale, activity.last

    body, stale, touched = asyncio.run(scenario())
    assert body == "hello"
    assert touched > stale


def test_watch_for_input_stops_once_nothing_has_happened_for_a_while(db, run, monkeypatch):
    monkeypatch.setattr(runner_module, "INPUT_POLL_SECONDS", 0.01)

    async def scenario():
        activity = runner_module._Activity()
        activity.last -= 10  # already well past any idle window
        watcher = runner_module._watch_for_input(db, run.id, activity, idle_seconds=0.05)
        return [body async for body in watcher]

    assert asyncio.run(scenario()) == []


def test_watch_for_input_does_not_miss_a_row_committed_near_the_idle_deadline(
    db, run, data_dir, monkeypatch
):
    """Regression for a real race: checking the idle deadline *before*
    fetching would let a row committed through a genuinely separate
    connection go unseen, even though it was already there when this loop was
    going to look — found by an actual concurrent smoke test, not assumed safe
    from a single-session test.

    The state that race produces is an iteration where the deadline has
    already passed *and* an unread row exists, so that is the state this sets
    up directly. It used to be reached by racing a thread into the gap between
    two poll ticks, which meant the assertion held only while a wall-clock
    write landed inside an 80ms window — thread start, sleep overshoot, engine
    connect and a WAL commit all had to fit, and on a loaded runner they did
    not. It failed in CI having passed on the same commit minutes earlier.

    Nothing about the guarantee is weakened by removing the thread. The
    ordering under test is between two statements in one iteration, not
    between two threads; the separate connection is kept because *that* part
    is a real question, being what proves this session sees another's commit
    at all.
    """
    from sqlalchemy.orm import Session as SessionCls

    from workbench.database.db import make_engine
    from workbench.runs.store import append_input

    monkeypatch.setattr(runner_module, "INPUT_POLL_SECONDS", 0.01)
    other_engine = make_engine(f"sqlite+pysqlite:///{data_dir / 'workbench.db'}")
    with SessionCls(other_engine) as other_db:
        append_input(other_db, run.id, "from elsewhere")

    async def scenario():
        activity = runner_module._Activity()
        # Already past the window. A loop that checked this before fetching
        # would return here having never looked, which is the bug.
        activity.last -= 10
        watcher = runner_module._watch_for_input(db, run.id, activity, idle_seconds=0.05)
        return [body async for body in watcher]

    assert asyncio.run(scenario()) == ["from elsewhere"]


class InputCapturingBackend:
    """A fake that actually drains `request.inputs`, unlike the usual
    `FakeBackend`, which is what makes it possible to prove a typed message
    reaches the backend at all rather than just that the plumbing compiles."""

    name = "fake"

    def __init__(self, outcome):
        self._outcome = outcome
        self.received: list[str] = []

    async def run(self, request):
        if request.inputs is not None:
            async for text in request.inputs:
                self.received.append(text)
        yield self._outcome


def test_a_typed_message_reaches_the_backend_through_a_real_run(db, task, checkout, monkeypatch):
    """A conversation, because that is now the only phase that listens."""
    from workbench.runs.store import append_input

    run = create_conversation(db, task.project, backend="fake")
    append_input(db, run.id, "actually, also check the tests")
    fake = InputCapturingBackend(AgentFinished(text="done"))
    monkeypatch.setattr(runner_module, "get_backend", lambda _n: fake)
    monkeypatch.setattr(runner_module, "input_idle_seconds", lambda: 0.3)
    monkeypatch.setattr(runner_module, "INPUT_POLL_SECONDS", 0.02)

    execute(db, run)

    assert fake.received == ["actually, also check the tests"]
    assert run.status is RunStatus.SUCCEEDED


def test_a_plan_or_execute_run_does_not_wait_to_be_typed_at(db, run, checkout, monkeypatch):
    """It has delivered its result and nobody is expected to answer.

    Waiting held the run `running` long after it was done, kept one of two
    concurrency slots, and delayed the pull request by the whole window — for
    a message that could not have reached the agent anyway, since inputs are
    only pulled between turns.
    """
    fake = InputCapturingBackend(AgentFinished(text="done"))
    monkeypatch.setattr(runner_module, "get_backend", lambda _n: fake)
    monkeypatch.setattr(
        runner_module,
        "input_idle_seconds",
        lambda: pytest.fail("an execute run consulted the idle window"),
    )

    execute(db, run)

    assert run.status is RunStatus.SUCCEEDED


# --- A project's own conversation -------------------------------------------


def test_a_conversation_runs_with_no_task_at_all(db, task, checkout, backend):
    """The whole point: it belongs to the project directly."""
    run = create_conversation(db, task.project, backend="fake")

    execute(db, run)

    assert run.status is RunStatus.SUCCEEDED
    assert run.task_id is None
    assert run.project_id == task.project.id


def test_a_conversation_skips_worktree_setup_entirely(db, task, checkout, backend):
    """No fetch, no branch, no setup command — nothing task-scoped to
    isolate, which is also what makes its first message faster."""
    run = create_conversation(db, task.project, backend="fake")

    execute(db, run)

    assert backend.requests[0].worktree == checkout


def test_a_conversation_reports_the_same_missing_clone_message(db, task):
    """Not cloned is not cloned, whichever kind of run asks."""
    run = create_conversation(db, task.project, backend="fake")

    execute(db, run)

    assert run.status is RunStatus.FAILED
    assert "cloned" in (run.error or "")


def test_a_conversation_resumes_its_own_earlier_session(db, task, checkout, backend):
    first = create_conversation(db, task.project, backend="fake")
    finish_run(db, first, RunStatus.SUCCEEDED, resume_token="session-from-last-time")
    second = create_conversation(db, task.project, backend="fake")

    execute(db, second)

    assert backend.requests[0].resume_token == "session-from-last-time"


def test_a_conversations_resume_token_is_not_a_tasks(db, task, checkout, backend):
    """Scoped to the project, not shared with — or by — any one task's runs."""
    task_run = create_run(db, task, RunPhase.EXECUTE, backend="fake")
    finish_run(db, task_run, RunStatus.SUCCEEDED, resume_token="belongs-to-the-task")

    assert resume_token_for_project(db, task.project.id, "fake") is None


def test_the_most_recent_conversation_token_wins(db, task):
    first = create_conversation(db, task.project, backend="fake")
    finish_run(db, first, RunStatus.FAILED, resume_token="older")
    second = create_conversation(db, task.project, backend="fake")
    finish_run(db, second, RunStatus.SUCCEEDED, resume_token="newer")

    assert resume_token_for_project(db, task.project.id, "fake") == "newer"


def test_a_conversation_leaves_no_task_touched_on_failure(db, task, checkout, monkeypatch):
    from workbench.agents.protocol import AgentFailed

    monkeypatch.setattr(
        runner_module, "get_backend", lambda _n: FakeBackend(outcome=AgentFailed("boom"))
    )
    run = create_conversation(db, task.project, backend="fake")

    execute(db, run)

    assert run.status is RunStatus.FAILED


def test_a_second_message_reaches_a_running_conversation_without_a_second_process(
    db, task, checkout, monkeypatch
):
    """The whole point of task 26's snappiness requirement, proven rather
    than assumed to carry over from the task case: once a conversation's
    process is up, a message typed in mid-stream reaches that same backend
    call through the input queue — nothing here starts a second run."""
    from workbench.runs.store import append_input

    fake = InputCapturingBackend(AgentFinished(text="done"))
    monkeypatch.setattr(runner_module, "get_backend", lambda _n: fake)
    monkeypatch.setattr(runner_module, "input_idle_seconds", lambda: 0.3)
    monkeypatch.setattr(runner_module, "INPUT_POLL_SECONDS", 0.02)
    run = create_conversation(db, task.project, backend="fake")
    append_input(db, run.id, "and also close out the stale ones")

    execute(db, run)

    assert fake.received == ["and also close out the stale ones"]
    assert run.status is RunStatus.SUCCEEDED
    assert run.task_id is None
    assert run.project_id == task.project.id
    assert task.status is TaskStatus.OPEN


# --- The entry point -------------------------------------------------------


def test_main_refuses_a_run_that_is_already_going(db, run, checkout, backend):
    """Two processes on one run would interleave events and both write an outcome."""
    run.status = RunStatus.RUNNING
    db.commit()

    assert main([str(run.id)]) == 1


def test_main_refuses_an_id_that_does_not_exist(data_dir, db):
    assert main(["9999"]) == 1


def test_main_rejects_a_missing_or_malformed_argument(data_dir):
    assert main([]) == 2
    assert main(["not-a-number"]) == 2


def test_main_strips_metered_api_credentials(db, run, checkout, backend, monkeypatch):
    """The subscription decision, enforced at the one point every backend inherits."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-survive")

    main([str(run.id)])

    assert "ANTHROPIC_API_KEY" not in os.environ


def test_main_keeps_the_key_when_api_billing_is_asked_for(db, run, checkout, backend, monkeypatch):
    """Opting into metered billing has to be possible, just never accidental."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-deliberate")
    monkeypatch.setenv("WORKBENCH_BILLING", "api")

    main([str(run.id)])

    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-deliberate"


def test_main_runs_a_queued_run_to_completion(db, run, checkout, backend):
    assert main([str(run.id)]) == 0

    db.refresh(run)
    assert run.status is RunStatus.SUCCEEDED


def test_record_translates_an_interruption_without_a_worktree(db, run):
    """A run signalled before `prepare` finished has no diff to take."""
    runner_module.record(db, run, Interrupted("SIGTERM"))

    assert run.status is RunStatus.CANCELLED
    assert run.diffstat is None


def test_main_leaves_the_rest_of_the_environment_intact(db, run, checkout, backend, monkeypatch):
    """Stripping credentials must not become sanitising everything.

    Under a subscription the credential is found through HOME, so a runner that
    cleared its own environment would authenticate as nobody — and would fail
    in a way that looks like a missing login rather than a bug in Workbench.
    """
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    main([str(run.id)])

    assert os.environ["HOME"] == "/home/someone"
    assert os.environ["PATH"] == "/usr/bin:/bin"


# --- Continuing a task's finished run ----------------------------------------
#
# The counterpart to plan and execute ending the moment the agent is done.
# Ending promptly is only reasonable because the thread can be picked back up
# deliberately, and this is that path.


def _worked_task(db, run, checkout, backend):
    """A task that has been run once, so it has a worktree and a session."""
    execute(db, run)
    return run.task


def test_a_task_conversation_runs_in_that_tasks_worktree(db, run, checkout, backend):
    """Not the clone, the way a project conversation does. A session token is
    keyed to the directory it was issued in, so resuming anywhere else starts
    cold while looking identical."""
    from workbench.runs.store import create_task_conversation

    task = _worked_task(db, run, checkout, backend)
    followup = create_task_conversation(db, task, backend="fake")

    execute(db, followup)

    assert str(backend.requests[-1].worktree) == task.worktree_path
    assert backend.requests[-1].phase is RunPhase.CONVERSATION


def test_a_task_conversation_resumes_the_session_that_ran_the_task(db, run, checkout, backend):
    from workbench.runs.store import create_task_conversation

    task = _worked_task(db, run, checkout, backend)
    followup = create_task_conversation(db, task, backend="fake")

    execute(db, followup)

    assert backend.requests[-1].resume_token == run.resume_token


def test_a_task_conversation_says_it_is_a_follow_up_not_a_second_attempt(
    db, run, checkout, backend
):
    """A resumed agent reads a new prompt as a new instruction and starts
    working again — the opposite of what the button offered."""
    from workbench.runs.store import create_task_conversation

    task = _worked_task(db, run, checkout, backend)
    followup = create_task_conversation(db, task, backend="fake")

    execute(db, followup)

    assert "not a new attempt" in backend.requests[-1].prompt


def test_a_task_conversation_without_a_worktree_is_refused(db, task, checkout, backend):
    """Nothing has run this task, so there is no directory the session could
    be resumed in."""
    from workbench.runs.store import create_task_conversation

    followup = create_task_conversation(db, task, backend="fake")

    execute(db, followup)

    assert followup.status is RunStatus.FAILED
    assert "worktree" in (followup.error or "")


def test_a_task_conversation_leaves_the_tasks_status_alone(db, run, checkout, backend):
    """Talking about work is not doing it. `record` routes on phase before it
    reads a task at all, which is what keeps this true."""
    from workbench.runs.store import create_task_conversation

    task = _worked_task(db, run, checkout, backend)
    task.status = TaskStatus.OPEN
    db.commit()
    followup = create_task_conversation(db, task, backend="fake")

    execute(db, followup)

    assert followup.status is RunStatus.SUCCEEDED
    assert task.status is TaskStatus.OPEN


def test_a_seeded_conversation_opens_with_the_seed_message(db, run, checkout, backend):
    """The whole point of seeding one: skip the empty 'I'm here' round trip
    and let the agent see what was actually asked straight away."""
    from workbench.runs.store import create_task_conversation

    task = _worked_task(db, run, checkout, backend)
    followup = create_task_conversation(
        db, task, backend="fake", seed_message="Please check CI on this branch."
    )

    execute(db, followup)

    assert "Please check CI on this branch." in backend.requests[-1].prompt


def test_an_unseeded_conversation_still_gets_the_generic_check_in(db, run, checkout, backend):
    """`seed_message` is optional — leaving it unset must not change today's
    behavior for a bare 'continue this conversation' click."""
    from workbench.runs.store import create_task_conversation

    task = _worked_task(db, run, checkout, backend)
    followup = create_task_conversation(db, task, backend="fake")

    execute(db, followup)

    assert "not a new attempt" in backend.requests[-1].prompt
