"""The remote-control wrapper and its unit's own convergence, without a real
pty or a real systemd.

`workbench.remote_control.main` is the thing `ExecStart=` actually runs — see
that module for why it exists rather than invoking the CLI directly. These
pin the three things worth getting wrong: no CLI found, an ordinary exit, and
a signal-terminated child folding into the same plain non-zero rather than
something more precise nobody asked for.

`install.converge_remote_control_unit` is the deploy-time half: restart only
when the unit's own name is in the `changed` set it was handed, never on an
unrelated change elsewhere in the app — see the function's own docstring for
why that restraint matters for a unit that may be a person's live session.
"""

from workbench import install, remote_control


def waitstatus(*, exit_code: int | None = None, signal: int | None = None) -> int:
    """A raw `os.wait()`-style status, encoded the way the kernel actually
    does it — exit code in the high byte, signal number in the low seven bits
    — so these tests exercise the real `os.WIFEXITED`/`os.WEXITSTATUS` rather
    than a stand-in for them."""
    if signal is not None:
        return signal
    return (exit_code or 0) << 8


def test_main_fails_without_a_cli(monkeypatch):
    monkeypatch.setattr(remote_control, "remote_control_argv", lambda: None)

    assert remote_control.main() == 1


def test_main_returns_the_wrapped_commands_exit_code(monkeypatch):
    monkeypatch.setattr(
        remote_control, "remote_control_argv", lambda: ["claude", "--remote-control"]
    )
    monkeypatch.setattr(remote_control.pty, "spawn", lambda argv: waitstatus(exit_code=2))

    assert remote_control.main() == 2


def test_main_folds_a_signal_into_a_plain_nonzero_exit(monkeypatch):
    """`pty.spawn`'s status is `os.wait`-shaped, not a plain exit code — a
    child killed by a signal is not `WIFEXITED`, and `Restart=always` only
    needs "did it exit", so this is reported as 1 rather than guessed at."""
    monkeypatch.setattr(
        remote_control, "remote_control_argv", lambda: ["claude", "--remote-control"]
    )
    monkeypatch.setattr(remote_control.pty, "spawn", lambda argv: waitstatus(signal=9))

    assert remote_control.main() == 1


def test_converge_remote_control_unit_restarts_only_its_own_unit(monkeypatch):
    monkeypatch.setattr(install, "remote_control_unit_name", lambda: "workbench-remote-control")
    calls: list[list[str]] = []
    monkeypatch.setattr(install, "run", lambda argv, **_kwargs: calls.append(argv))

    install.converge_remote_control_unit({"workbench-remote-control.service", "other.service"})

    assert calls == [["systemctl", "try-restart", "workbench-remote-control.service"]]


def test_converge_remote_control_unit_leaves_an_unchanged_unit_alone(monkeypatch):
    monkeypatch.setattr(install, "remote_control_unit_name", lambda: "workbench-remote-control")
    calls: list[list[str]] = []
    monkeypatch.setattr(install, "run", lambda argv, **_kwargs: calls.append(argv))

    install.converge_remote_control_unit({"other.service"})

    assert calls == []
