"""The switch that hands a node's GPU to the television, and back.

One card cannot hold `qwen3:8b` and a game, so the two are mutually exclusive.
What is worth testing is not that `systemctl` is called — it is the ordering and
the refusal to fail, because both were chosen against the obvious alternative:
the model server is stopped *before* the head is told, and nothing in here
raises, because the caller is Sunshine and the audience is somebody holding a
controller.
"""

from dataclasses import dataclass, field

import pytest

from workbench import gaming


@dataclass
class Switch:
    """What the switch did, without a systemd or a head behind it."""

    systemctl: list[list[str]] = field(default_factory=list)
    registered: int = 0
    #: Whether the model server is answering, which the fake systemctl moves so
    #: the bounded wait inside `start`/`stop` actually resolves.
    serving: bool = True


@pytest.fixture
def switch(monkeypatch) -> Switch:
    state = Switch()

    def systemctl(*arguments: str) -> bool:
        state.systemctl.append(list(arguments))
        if arguments[0] == "stop":
            state.serving = False
        if arguments[0] == "start":
            state.serving = True
        return True

    def registered() -> None:
        state.registered += 1

    monkeypatch.setattr(gaming, "_systemctl", systemctl)
    monkeypatch.setattr(gaming, "_endpoint_answers", lambda *a, **k: state.serving)
    monkeypatch.setattr(gaming, "register_with_head", registered)
    return state


def test_starting_stops_the_model_server_then_tells_the_head(switch):
    """The order is the whole claim. Registering first would announce a node
    that still had five gigabytes of weights resident."""
    assert gaming.start() == 0

    assert switch.systemctl == [["stop", gaming.INFERENCE_UNIT]]
    assert switch.registered == 1
    assert switch.serving is False


def test_stopping_starts_the_model_server_and_tells_the_head(switch):
    switch.serving = False

    assert gaming.stop() == 0

    assert switch.systemctl == [["start", gaming.INFERENCE_UNIT]]
    assert switch.registered == 1
    assert switch.serving is True


def test_a_server_that_will_not_let_go_is_a_warning_not_a_failure(monkeypatch, caplog):
    """The game launches on a card with a model still on it — degraded, and
    better than refusing to start the game at all."""
    monkeypatch.setattr(gaming, "_systemctl", lambda *a: False)
    monkeypatch.setattr(gaming, "_endpoint_answers", lambda *a, **k: True)
    monkeypatch.setattr(gaming, "register_with_head", lambda: None)
    monkeypatch.setattr(gaming, "SETTLE_TIMEOUT_SECONDS", 0.01)

    with caplog.at_level("WARNING"):
        assert gaming.start() == 0

    assert "short of VRAM" in caplog.text


def test_a_server_that_does_not_come_back_still_registers(monkeypatch, caplog):
    """A registration saying "still not serving" is the useful thing to send.
    Staying quiet would leave the head believing the node had recovered, and it
    would send it the next run."""
    registered = []
    monkeypatch.setattr(gaming, "_systemctl", lambda *a: True)
    monkeypatch.setattr(gaming, "_endpoint_answers", lambda *a, **k: False)
    monkeypatch.setattr(gaming, "register_with_head", lambda: registered.append(1))
    monkeypatch.setattr(gaming, "SETTLE_TIMEOUT_SECONDS", 0.01)

    with caplog.at_level("WARNING"):
        assert gaming.stop() == 0

    assert registered == [1]
    assert "stays withdrawn" in caplog.text


def test_systemctl_that_cannot_be_run_at_all_is_survivable(monkeypatch, caplog):
    """No traceback in front of somebody at a television."""

    def missing(*args, **kwargs):
        raise OSError("no systemctl here")

    monkeypatch.setattr(gaming.subprocess, "run", missing)

    with caplog.at_level("WARNING"):
        assert gaming._systemctl("stop", "ollama.service") is False

    assert "Could not run" in caplog.text


def test_a_refusing_systemctl_is_reported_with_what_it_said(monkeypatch, caplog):
    class Refused:
        returncode = 1
        stdout = ""
        stderr = "Interactive authentication required."

    monkeypatch.setattr(gaming.subprocess, "run", lambda *a, **k: Refused())

    with caplog.at_level("WARNING"):
        assert gaming._systemctl("stop", "ollama.service") is False

    assert "Interactive authentication required." in caplog.text


@pytest.mark.parametrize("action", ["start", "stop"])
def test_the_module_is_reachable_as_a_command(monkeypatch, action):
    """This is how the unit invokes it, so it is worth one assertion."""
    called = []
    monkeypatch.setattr(gaming, "start", lambda: called.append("start") or 0)
    monkeypatch.setattr(gaming, "stop", lambda: called.append("stop") or 0)

    assert gaming.main([action]) == 0
    assert called == [action]


def test_an_unknown_action_is_refused(monkeypatch):
    with pytest.raises(SystemExit):
        gaming.main(["pause"])
