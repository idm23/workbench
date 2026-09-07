"""Which run the task tree describes.

A task accumulates runs, and the tree shows one of them. Picking the wrong
one is silent — the page renders, the badge looks plausible, and the actions
it offers belong to work that finished long ago. That is the failure this
module exists to prevent, so most of what is pinned here is which run wins
rather than how it is rendered.
"""

import pytest

from workbench.database.models import Project, RunPhase, RunStatus, Task, User
from workbench.runs.activity import activity_by_task
from workbench.runs.store import create_run, finish_run


def plan(db, task, status: RunStatus, **finish):
    """A plan run left in `status`. Queued runs are left as created, since
    `finish_run` always stamps `finished_at`."""
    run = create_run(db, task, RunPhase.PLAN, backend="fake")
    if status is not RunStatus.QUEUED:
        finish_run(db, run, status, **finish)
    return run


def test_the_only_run_is_the_one_described(db, task):
    run = plan(db, task, RunStatus.AWAITING_REVIEW)

    activity = activity_by_task(db, task.project_id)

    assert activity[task.id].run_id == run.id
    assert activity[task.id].status is RunStatus.AWAITING_REVIEW


def test_the_newest_run_wins_among_several_marked_ones(db, task):
    plan(db, task, RunStatus.FAILED)
    newer = plan(db, task, RunStatus.AWAITING_REVIEW)

    assert activity_by_task(db, task.project_id)[task.id].run_id == newer.id


def test_a_succeeded_run_supersedes_an_earlier_awaiting_review(db, task):
    """The bug this module was written for.

    `succeeded` is not a marked status, so filtering before picking the
    newest left the older `awaiting_review` describing the task forever —
    and the tree went on offering to approve a plan that two later runs had
    already carried out.
    """
    plan(db, task, RunStatus.AWAITING_REVIEW)
    finish_run(db, create_run(db, task, RunPhase.EXECUTE, backend="fake"), RunStatus.SUCCEEDED)

    assert task.id not in activity_by_task(db, task.project_id)


def test_a_task_whose_only_run_succeeded_is_not_marked(db, task):
    finish_run(db, create_run(db, task, RunPhase.EXECUTE, backend="fake"), RunStatus.SUCCEEDED)

    assert activity_by_task(db, task.project_id) == {}


def test_a_cancelled_run_also_supersedes_an_earlier_plan(db, task):
    """Cancelled is not marked either, and for the same reason: whatever the
    person did last is what the page should be describing."""
    plan(db, task, RunStatus.AWAITING_REVIEW)
    finish_run(db, create_run(db, task, RunPhase.EXECUTE, backend="fake"), RunStatus.CANCELLED)

    assert task.id not in activity_by_task(db, task.project_id)


@pytest.mark.parametrize(
    "status",
    [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.AWAITING_REVIEW, RunStatus.FAILED],
)
def test_every_marked_status_is_still_marked_when_it_is_newest(db, task, status):
    """The other half: superseding must not quietly stop marking the states
    that a person actually needs to see."""
    run = plan(db, task, status)

    assert activity_by_task(db, task.project_id)[task.id].run_id == run.id


def test_a_retry_after_a_success_is_marked_again(db, task):
    """Superseding is about recency, not about a success being terminal for
    the task — starting another run has to light the row back up."""
    finish_run(db, create_run(db, task, RunPhase.EXECUTE, backend="fake"), RunStatus.SUCCEEDED)
    retry = create_run(db, task, RunPhase.EXECUTE, backend="fake")

    assert activity_by_task(db, task.project_id)[task.id].run_id == retry.id


def test_another_projects_runs_are_not_included(db, task):
    """The widened query drops the status filter, so the project filter is
    now the only thing scoping it."""
    stranger = Project(
        user=User(name="someone-else"),
        owner="idm23",
        repo="other",
        github_url="https://github.com/idm23/other",
    )
    theirs = Task(project=stranger, title="Not ours")
    db.add(theirs)
    db.commit()
    plan(db, theirs, RunStatus.AWAITING_REVIEW)

    assert activity_by_task(db, task.project_id) == {}
