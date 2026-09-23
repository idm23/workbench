"""Which account a run bills.

This lives in `config.py` rather than in a backend because it is Workbench's
decision, not a vendor's, and because the failure it guards against is silent.
An `ANTHROPIC_API_KEY` reaching the service — exported in a shell, inherited
from a parent process, or added to `/etc/workbench/env` for something else
entirely — would move every run onto metered billing with nothing in Workbench
changing to show it. The first sign would be an invoice.
"""

from pathlib import Path

from workbench.config import (
    agent_database,
    agent_environment,
    billing_mode,
    bills_subscription,
)


def test_subscription_is_the_default(monkeypatch):
    monkeypatch.delenv("WORKBENCH_BILLING", raising=False)

    assert billing_mode() == "subscription"
    assert bills_subscription() is True


def test_an_api_key_is_stripped_under_a_subscription(monkeypatch):
    monkeypatch.delenv("WORKBENCH_BILLING", raising=False)

    env = agent_environment({"ANTHROPIC_API_KEY": "sk-test", "HOME": "/home/ian"})

    assert "ANTHROPIC_API_KEY" not in env


def test_an_auth_token_is_stripped_too(monkeypatch):
    """The other spelling of the same credential."""
    monkeypatch.delenv("WORKBENCH_BILLING", raising=False)

    assert "ANTHROPIC_AUTH_TOKEN" not in agent_environment({"ANTHROPIC_AUTH_TOKEN": "x"})


def test_the_subscription_credential_is_left_alone(monkeypatch):
    """It is found through HOME, so removing that would authenticate as nobody."""
    monkeypatch.delenv("WORKBENCH_BILLING", raising=False)

    env = agent_environment({"HOME": "/home/ian", "PATH": "/usr/bin"})

    assert env == {"HOME": "/home/ian", "PATH": "/usr/bin"}


def test_metered_billing_has_to_be_asked_for_out_loud(monkeypatch):
    monkeypatch.setenv("WORKBENCH_BILLING", "api")

    env = agent_environment({"ANTHROPIC_API_KEY": "sk-test"})

    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert bills_subscription() is False


def test_the_setting_is_case_and_whitespace_tolerant(monkeypatch):
    """It gets typed into a unit file by hand."""
    monkeypatch.setenv("WORKBENCH_BILLING", "  API  ")

    assert bills_subscription() is False


def test_an_unrecognised_value_stays_on_the_subscription(monkeypatch):
    """A typo should not silently start spending money per token."""
    monkeypatch.setenv("WORKBENCH_BILLING", "sbscription")

    assert bills_subscription() is True


def test_the_returned_environment_is_a_copy(monkeypatch):
    """Callers mutate it; the process's own environment must not follow."""
    monkeypatch.delenv("WORKBENCH_BILLING", raising=False)
    base = {"HOME": "/home/ian"}

    agent_environment(base)["HOME"] = "/elsewhere"

    assert base == {"HOME": "/home/ian"}


def test_an_agent_never_sees_this_instances_database(monkeypatch, tmp_path):
    """The runner writes to the real database; what the agent runs must not (#87).

    Overridden rather than removed: unset, the deployment's own code falls back
    to `repo_root()`, which is the real database again.
    """
    production = tmp_path / "data" / "workbench.db"
    monkeypatch.setenv("WORKBENCH_DB", str(production))
    worktree = tmp_path / "data" / "worktrees" / "task-9-thing"

    env = agent_environment({"WORKBENCH_DB": str(production)}, worktree=worktree)

    assert env["WORKBENCH_DB"] != str(production)
    assert env["WORKBENCH_DB"] == str(agent_database(worktree))
    assert not Path(env["WORKBENCH_DB"]).is_relative_to(worktree)
    assert Path(env["WORKBENCH_DB"]).parent.is_dir()


def test_the_runner_keeps_the_real_database(monkeypatch, tmp_path):
    """Without a worktree this is the runner pruning its own environment."""
    monkeypatch.delenv("WORKBENCH_BILLING", raising=False)
    production = str(tmp_path / "workbench.db")

    assert agent_environment({"WORKBENCH_DB": production})["WORKBENCH_DB"] == production


def test_each_worktree_gets_its_own_scratch_database(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "workbench.db"))

    first = agent_database(tmp_path / "task-1-one")
    second = agent_database(tmp_path / "task-2-two")

    assert first != second
    assert agent_database(tmp_path / "task-1-one") == first
