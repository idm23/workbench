"""Resolving a backend name.

The precedence — an explicit name, else this machine's default — is what
`projects.agent_backend` overriding `WORKBENCH_AGENT_BACKEND` amounts to, so
it is pinned here rather than left to the one caller that happens to use it.
"""

from workbench.agents.protocol import Backend
from workbench.agents.registry import UnknownBackend, available_backends, get_backend
from workbench.agents.tests.fake import FakeBackend


def test_the_default_backend_resolves():
    assert get_backend("claude").name == "claude"


def test_no_name_means_this_machines_default(monkeypatch):
    monkeypatch.setenv("WORKBENCH_AGENT_BACKEND", "claude")

    assert get_backend().name == "claude"


def test_an_unknown_name_is_a_result_not_an_exception():
    result = get_backend("gpt-9")

    assert isinstance(result, UnknownBackend)
    assert result.name == "gpt-9"


def test_the_message_lists_what_is_available():
    """The realistic cause is a typo, and naming the alternatives ends it."""
    result = get_backend("clyde")

    assert isinstance(result, UnknownBackend)
    assert "claude" in result.message


def test_an_unknown_default_does_not_fall_back_silently(monkeypatch):
    """A misconfigured machine should say so, not quietly run something else."""
    monkeypatch.setenv("WORKBENCH_AGENT_BACKEND", "typo")

    assert isinstance(get_backend(), UnknownBackend)


def test_available_backends_is_sorted():
    assert list(available_backends()) == sorted(available_backends())


def test_the_protocol_is_implementable_by_something_that_is_not_claude():
    """The claim the seam makes, asserted rather than assumed."""
    assert isinstance(FakeBackend(), Backend)
