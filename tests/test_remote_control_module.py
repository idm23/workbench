"""Tests for the :mod:`workbench.remote_control` utility and its associated
systemd unit deployment helper.

The original implementation includes a small ``main()`` function that resolves
the Claude CLI binary, spawns it via a pseudo‑terminal so the CLI can do its
interactive bootstrap, and translates the ``pty.spawn`` ``os.wait``-style status
into an exit code suitable for ``systemd``.

These tests exercise the control flow that is left untested in the
application: returning the correct exit status in three scenarios and
verifying that :func:`converge_remote_control_unit` triggers a ``try-restart``
only when its unit changed.

The tests use ``pytest`` monkeypatch to replace external dependencies such
as the real ``pty.spawn`` and the configuration helper that determines the
unit name.
"""

import os
from types import SimpleNamespace

import pytest

from workbench import remote_control, install, config


# ---------------------------------------------------------------------------
# Tests for remote_control.main()
# ---------------------------------------------------------------------------

def _status(exit_code: int) -> int:
    """Return a raw ``os.wait()`` status code for the given *exit_code*.

    ``pty.spawn`` returns an integer where the high byte holds the exit code
    and the low byte holds the signal. This helper produces that value.
    """
    return exit_code << 8


@pytest.mark.parametrize(
    "argv,spawn_return,expected",
    [
        (None, None, 1),  # no CLI found
        (['fakecli', '--remote-control'], _status(0), 0),  # normal exit
        (['fakecli', '--remote-control'], _status(2), 2),  # non‑zero exit
    ],
)
def test_remote_control_main(monkeypatch, argv, spawn_return, expected):
    """Verify that :func:`remote_control.main` returns the right exit status.

    The test patches :func:`remote_control.remote_control_argv` and
    :func:`pty.spawn` directly and asserts that the resulting exit code is
    as expected.  ``configure_console_logging`` is replaced with a no‑op to
    avoid side effects on the test logger.
    """

    # Stub out the configuration helpers
    monkeypatch.setattr(remote_control, "remote_control_argv", lambda: argv)
    monkeypatch.setattr(remote_control, "configure_console_logging", lambda: None)

    # Replace pty.spawn – it may not be called when argv is None.
    def fake_spawn(_argv):
        if spawn_return is None:
            raise RuntimeError("pty.spawn should not have been called")
        return spawn_return

    # Patch status helpers to interpret our synthetic status values.
    monkeypatch.setattr(os, "WIFEXITED", lambda status: (status & 0x7f) == 0)
    monkeypatch.setattr(os, "WEXITSTATUS", lambda status: status >> 8)

    # Replace the pty.spawn call used by the module.
    monkeypatch.setattr("pty.spawn", fake_spawn)

    # Run main and verify the outcome
    rc = remote_control.main()
    assert rc == expected


# ---------------------------------------------------------------------------
# Tests for install.converge_remote_control_unit
# ---------------------------------------------------------------------------

def test_converge_remote_control_unit_triggers_restart(monkeypatch):
    """If the unit changed, :func:`converge_remote_control_unit` should
    issue a ``systemctl try-restart``.
    """

    # Replace the run function and capture its arguments
    run_calls = []
    monkeypatch.setattr(install, "run", lambda argv, **kw: run_calls.append(argv))
    monkeypatch.setattr(install, "info", lambda *_, **__: None)

    # Force a known unit name
    monkeypatch.setattr(config, "remote_control_unit_name", lambda: "wry-remote")
    unit = "wry-remote.service"
    changed = {unit, "other.service"}
    install.converge_remote_control_unit(changed)
    assert run_calls == [["systemctl", "try-restart", unit]]


def test_converge_remote_control_unit_no_change(monkeypatch):
    """If the unit did not change the function should do nothing."""

    run_calls: list = []
    monkeypatch.setattr(install, "run", lambda argv, **kw: run_calls.append(argv))
    monkeypatch.setattr(install, "info", lambda *_, **__: None)
    monkeypatch.setattr(config, "remote_control_unit_name", lambda: "wry-remote")
    install.converge_remote_control_unit({"other.service"})
    assert run_calls == []
