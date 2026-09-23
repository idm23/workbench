"""The first line of every run says what it spends, and that is the backend's answer.

It used to quote `config.billing_mode` for every backend, so a run on a GPU in
the next room announced that it was billing a subscription.
"""

from workbench.agents.claude import ClaudeBackend
from workbench.agents.local import LocalBackend
from workbench.agents.registry import UnknownBackend
from workbench.agents.tests.fake import FakeBackend
from workbench.database.models import RunEvent, RunEventKind
from workbench.runs import runner as runner_module
from workbench.runs.runner import execute


def first_notice(db, run) -> str:
    event = (
        db.query(RunEvent)
        .filter_by(run_id=run.id, kind=RunEventKind.NOTICE)
        .order_by(RunEvent.seq)
        .first()
    )
    assert event is not None
    return event.payload["text"]


def test_the_runner_quotes_the_backend(db, run, checkout, monkeypatch):
    fake = FakeBackend(billing_notice="spending a GPU and a wall clock, billing nothing")
    monkeypatch.setattr(runner_module, "get_backend", lambda _name: fake)

    execute(db, run)

    assert first_notice(db, run) == (
        f"Backend {run.backend}, spending a GPU and a wall clock, billing nothing."
    )


def test_an_unknown_backend_claims_to_spend_nothing(db, run, checkout, monkeypatch):
    """`prepare` is what explains an unknown backend; the notice only names it."""
    unknown = UnknownBackend("nonesuch", ("claude", "local"))
    monkeypatch.setattr(runner_module, "get_backend", lambda _name: unknown)

    execute(db, run)

    assert first_notice(db, run) == f"Backend {run.backend}."


def test_a_local_run_mentions_no_subscription():
    assert "subscription" not in LocalBackend().billing_notice


def test_a_claude_run_follows_the_billing_mode(monkeypatch):
    monkeypatch.delenv("WORKBENCH_BILLING", raising=False)
    assert ClaudeBackend().billing_notice == "billing a Claude subscription"

    monkeypatch.setenv("WORKBENCH_BILLING", "api")
    assert ClaudeBackend().billing_notice == "billing the metered API"
