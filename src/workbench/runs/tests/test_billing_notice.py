"""Test that the runner reports the correct billing notice for a local run.

The original issue was that the runner always reported a subscription based
notice for every backend, even the local GPU backend that does not bill
anything.  The :func:`workbench.agents.local.LocalBackend.billing_notice`
property now returns a string describing the actual consumption, and the
runner should forward that string.

This test exercises the end-to-end code path that creates a run, executes the
runner, records a ``RunEventKind.NOTICE`` event, and then inspects the
payload to ensure that the notice does not mention a subscription.  It also
confirms that the text contains the expected phrase ``spends a GPU`` and that
the notice ends with a period.
"""

import pytest

from workbench.agents.local import LocalBackend
from workbench.database.models import RunEvent, RunEventKind, RunPhase
from workbench.runs.runner import execute
from workbench.runs.store import create_run


@pytest.fixture
def run_local(db, task):
    """Create a run that uses the local backend."""
    return create_run(db, task, RunPhase.EXECUTE, backend="local")


def test_local_run_billing_notice(db, run_local, monkeypatch):
    """The runner should emit a notice that does not mention a subscription."""
    # Ensure the registry uses the local backend implementation.
    monkeypatch.setattr(
        "workbench.agents.registry.get_backend",
        lambda name: LocalBackend(),
    )
    execute(db, run_local)

    events = db.query(RunEvent).filter_by(run_id=run_local.id).order_by(RunEvent.seq).all()
    notice_texts = [e.payload["text"] for e in events if e.kind is RunEventKind.NOTICE]
    assert notice_texts, "No notice event emitted"
    notice = notice_texts[0]
    assert "subscription" not in notice, "Local run should not mention a subscription"
    assert "spends a GPU" in notice, "Billing notice should describe GPU usage"
    assert notice.endswith("."), "Notice should end with a period"
