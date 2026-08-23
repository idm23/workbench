"""Which account a run bills.

This lives in `config.py` rather than in a backend because it is Workbench's
decision, not a vendor's, and because the failure it guards against is silent.
An `ANTHROPIC_API_KEY` reaching the service — exported in a shell, inherited
from a parent process, or added to `/etc/workbench/env` for something else
entirely — would move every run onto metered billing with nothing in Workbench
changing to show it. The first sign would be an invoice.
"""

from workbench.config import agent_environment, billing_mode, bills_subscription


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
