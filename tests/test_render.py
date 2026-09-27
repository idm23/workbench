"""The render surface a gaming node gives Steam and Sunshine to capture from.

Everything here is a step that talks to the machine — Xorg, a systemd system
unit — so what can be tested is the shape of what it would do and what it does
when the machine cannot oblige, the same discipline `test_install_node.py`
already holds itself to for the model server.
"""

import os
import pwd

import pytest

from workbench import render
from workbench.config import GAMESCOPE, X11_DUMMY
from workbench.install import InstallError


def test_install_dispatches_on_the_declared_backend(monkeypatch):
    monkeypatch.setattr(render, "render_backend", lambda: GAMESCOPE)
    monkeypatch.setattr(render, "_install_gamescope", lambda: "gamescope-called")
    monkeypatch.setattr(
        render, "_install_x11_dummy", lambda: pytest.fail("wrong backend dispatched")
    )

    assert render.install() == "gamescope-called"


def test_the_default_backend_dispatches_to_x11_dummy(monkeypatch):
    monkeypatch.setattr(render, "render_backend", lambda: X11_DUMMY)
    monkeypatch.setattr(render, "_install_x11_dummy", lambda: "x11-called")
    monkeypatch.setattr(
        render, "_install_gamescope", lambda: pytest.fail("wrong backend dispatched")
    )

    assert render.install() == "x11-called"


def test_gamescope_is_not_implemented_yet(caplog):
    """Stubbed on purpose — see the module docstring for why x11-dummy is the
    default rather than this."""
    with caplog.at_level("WARNING"):
        assert render._install_gamescope() is False

    assert "not implemented" in caplog.text


def test_no_systemd_skips_x11_rather_than_failing(monkeypatch, caplog):
    """The container path, mirroring the model server's and the gaming
    switch's — the install still has to finish and say what a real gaming
    node would have got."""
    monkeypatch.setattr(render, "systemd_is_running", lambda: False)

    with caplog.at_level("INFO"):
        assert render._install_x11_dummy() is False

    assert "no render surface" in caplog.text.lower()


def test_xorg_conf_is_written_only_when_it_changed(monkeypatch, tmp_path):
    target = tmp_path / "xorg-dummy.conf"
    monkeypatch.setattr(render, "X11_CONF_PATH", target)
    monkeypatch.setattr(render, "render_unit", lambda name: "rendered content\n")

    written = []
    monkeypatch.setattr(
        render, "write_privileged", lambda path, content, *, staged_as: written.append(path)
    )

    render._write_xorg_conf()
    assert written == [target]

    target.write_text("rendered content\n")
    render._write_xorg_conf()
    assert written == [target]  # unchanged: no second write


def test_x11_unit_is_written_to_the_system_dir_only_when_changed(monkeypatch, tmp_path):
    """Found the hard way: a `systemd --user` unit can never run as root, and
    opening the VT this needs is root-only on a machine with no setuid
    `Xorg.wrap`. See the module docstring for why this is `SYSTEMD_DIR`, the
    same directory `workbench-gaming.service` itself lands in, not a
    per-account user-unit directory."""
    target_dir = tmp_path / "system"
    monkeypatch.setattr(render, "SYSTEMD_DIR", target_dir)
    monkeypatch.setattr(render, "render_unit", lambda name: "rendered unit\n")

    written = []
    monkeypatch.setattr(
        render, "write_privileged", lambda path, content, *, staged_as: written.append(path)
    )

    expected = target_dir / render.X11_UNIT_NAME
    assert render._write_x11_unit() is True
    assert written == [expected]

    expected.write_text("rendered unit\n")
    assert render._write_x11_unit() is False
    assert written == [expected]  # unchanged: no second write


def test_enable_system_unit_needs_no_account_impersonation(monkeypatch):
    calls = []
    monkeypatch.setattr(render, "run", lambda argv, **k: calls.append(argv) or None)

    assert render._enable_system_unit() is True
    assert calls == [["systemctl", "enable", "--now", render.X11_UNIT_NAME]]


def test_a_raising_enable_is_a_warning_not_a_crash(monkeypatch, caplog):
    def fake_run(argv, **kwargs):
        raise InstallError("unit not found")

    monkeypatch.setattr(render, "run", fake_run)

    with caplog.at_level("WARNING"):
        assert render._enable_system_unit() is False

    assert render.X11_UNIT_NAME in caplog.text


def test_a_raising_xorg_install_is_a_warning_not_a_crash(monkeypatch, caplog):
    def fake_run(argv, **kwargs):
        raise InstallError("no space left on device")

    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    monkeypatch.setattr(render, "run", fake_run)

    with caplog.at_level("WARNING"):
        assert render._ensure_xorg_installed() is False

    assert "xserver-xorg-core" in caplog.text


def test_render_session_is_unknown_with_no_gaming_user(monkeypatch):
    monkeypatch.setattr(render, "gaming_user", lambda: None)
    assert render.render_session_is_up() is None


def test_render_session_is_false_for_the_unimplemented_gamescope_backend(monkeypatch):
    monkeypatch.setattr(render, "gaming_user", lambda: "ian")
    monkeypatch.setattr(render, "render_backend", lambda: GAMESCOPE)
    assert render.render_session_is_up() is False


def test_render_session_probes_the_system_unit_for_x11_dummy(monkeypatch):
    """A real probe now that the unit is system-level — no per-account
    impersonation needed, unlike the `systemd --user` shape this was first
    tried against."""

    class Active:
        stdout = "active\n"

    class Inactive:
        stdout = "inactive\n"

    monkeypatch.setattr(render, "gaming_user", lambda: "ian")
    monkeypatch.setattr(render, "render_backend", lambda: X11_DUMMY)

    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: Active())
    assert render.render_session_is_up() is True

    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: Inactive())
    assert render.render_session_is_up() is False


def test_display_environment_is_written_only_when_changed(monkeypatch, tmp_path):
    real = pwd.getpwuid(os.getuid())
    fake_account = pwd.struct_passwd(
        (
            real.pw_name,
            real.pw_passwd,
            real.pw_uid,
            real.pw_gid,
            real.pw_gecos,
            str(tmp_path),
            real.pw_shell,
        )
    )
    monkeypatch.setattr(render.os, "chown", lambda *a, **k: None)

    assert render._write_display_environment(fake_account) is True
    target = tmp_path / render.DISPLAY_ENV_PATH
    assert target.read_text() == "DISPLAY=:0\n"

    assert render._write_display_environment(fake_account) is False


def test_install_x11_dummy_sets_display_for_the_gaming_user(monkeypatch, tmp_path):
    """Nothing about the X server itself needs the gaming account, but
    Sunshine — a client of it, running as that account — has no other way to
    learn where the display is."""
    real = pwd.getpwuid(os.getuid())
    fake_account = pwd.struct_passwd(
        (
            real.pw_name,
            real.pw_passwd,
            real.pw_uid,
            real.pw_gid,
            real.pw_gecos,
            str(tmp_path),
            real.pw_shell,
        )
    )

    monkeypatch.setattr(render, "systemd_is_running", lambda: True)
    monkeypatch.setattr(render, "_ensure_xorg_installed", lambda: True)
    monkeypatch.setattr(render, "_write_xorg_conf", lambda: None)
    monkeypatch.setattr(render, "_write_x11_unit", lambda: False)
    monkeypatch.setattr(render, "_enable_system_unit", lambda: True)
    monkeypatch.setattr(render, "stop_blanking", lambda: True)
    monkeypatch.setattr(render, "gaming_user", lambda: real.pw_name)
    monkeypatch.setattr(render.pwd, "getpwnam", lambda name: fake_account)
    monkeypatch.setattr(render.os, "chown", lambda *a, **k: None)

    restarted = []
    monkeypatch.setattr(render, "restart_user_manager", lambda account: restarted.append(account))

    assert render._install_x11_dummy() is True
    assert (tmp_path / render.DISPLAY_ENV_PATH).read_text() == "DISPLAY=:0\n"
    assert restarted == [fake_account]


def test_install_x11_dummy_skips_display_setup_with_no_gaming_user(monkeypatch):
    monkeypatch.setattr(render, "systemd_is_running", lambda: True)
    monkeypatch.setattr(render, "_ensure_xorg_installed", lambda: True)
    monkeypatch.setattr(render, "_write_xorg_conf", lambda: None)
    monkeypatch.setattr(render, "_write_x11_unit", lambda: False)
    monkeypatch.setattr(render, "_enable_system_unit", lambda: True)
    monkeypatch.setattr(render, "gaming_user", lambda: None)
    monkeypatch.setattr(
        render, "restart_user_manager", lambda account: pytest.fail("no account to restart for")
    )

    assert render._install_x11_dummy() is True


def test_the_xorg_conf_never_blanks_the_display():
    """Xorg's defaults blank after ten idle minutes and then power the monitor
    down, and a stream carries no local input - so the node went black ten
    minutes into every session somebody was only watching."""
    conf = render.render_unit("xorg-dummy.conf.template")

    for flag in ("BlankTime", "StandbyTime", "SuspendTime", "OffTime"):
        assert f'Option "{flag}" "0"' in conf
    assert 'Option "DPMS" "false"' in conf


class _Xset:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


XSET_BLANKING = """Screen Saver:
  prefer blanking:  yes    allow exposures:  yes
  timeout:  600    cycle:  600
DPMS (Display Power Management Signaling):
  Standby: 600    Suspend: 600    Off: 600
  DPMS is Enabled
  Monitor is Off
"""

XSET_NEVER = """Screen Saver:
  prefer blanking:  no    allow exposures:  yes
  timeout:  0    cycle:  600
DPMS (Display Power Management Signaling):
  Server does not have the DPMS Extension
"""


def test_a_server_with_its_defaults_is_reported_as_blanking(monkeypatch):
    """What homebox-node-1 answered while its stream was solid black."""
    monkeypatch.setattr(render, "_xset", lambda *args: _Xset(XSET_BLANKING))
    assert render.display_blanks() is True


def test_a_server_told_never_to_blank_is_reported_as_such(monkeypatch):
    monkeypatch.setattr(render, "_xset", lambda *args: _Xset(XSET_NEVER))
    assert render.display_blanks() is False


def test_dpms_alone_still_counts_as_blanking(monkeypatch):
    """The screen saver off is not enough: DPMS powering the monitor down
    captures as the same black frame."""
    monkeypatch.setattr(
        render,
        "_xset",
        lambda *args: _Xset("timeout:  0    cycle:  600\n  DPMS is Enabled\n"),
    )
    assert render.display_blanks() is True


def test_a_server_that_cannot_be_asked_is_unknown(monkeypatch):
    monkeypatch.setattr(render, "_xset", lambda *args: None)
    assert render.display_blanks() is None


def test_converging_applies_no_blanking_to_a_running_server(monkeypatch):
    """Live, and on every tick: the configuration only takes effect at the
    server's next start, and restarting it would end whatever is on screen."""
    applied = []
    monkeypatch.setattr(render, "render_backend", lambda: X11_DUMMY)
    monkeypatch.setattr(render, "systemd_is_running", lambda: True)
    monkeypatch.setattr(render, "_write_xorg_conf", lambda: False)
    monkeypatch.setattr(render, "render_session_is_up", lambda: True)
    monkeypatch.setattr(render, "_xset", lambda *args: applied.append(args) or _Xset(""))
    monkeypatch.setattr(
        render, "run", lambda *a, **k: pytest.fail("converging must never restart the server")
    )

    render.converge()

    assert applied == [render.XSET_NEVER_BLANK]


def test_converging_leaves_a_stopped_server_alone(monkeypatch):
    monkeypatch.setattr(render, "render_backend", lambda: X11_DUMMY)
    monkeypatch.setattr(render, "systemd_is_running", lambda: True)
    monkeypatch.setattr(render, "_write_xorg_conf", lambda: False)
    monkeypatch.setattr(render, "render_session_is_up", lambda: False)
    monkeypatch.setattr(render, "_xset", lambda *args: pytest.fail("nobody to tell"))

    render.converge()


def test_a_node_without_xset_is_given_it(monkeypatch):
    """The doctor's question and the live fix both need `xset`, which a
    minimal server image does not have even when Xorg is installed."""
    installed = []
    monkeypatch.setattr(
        render.shutil, "which", lambda name: "/usr/bin/Xorg" if name == "Xorg" else None
    )
    monkeypatch.setattr(render, "run", lambda argv, **k: installed.append(argv))

    assert render._ensure_xorg_installed() is True
    assert installed == [["apt-get", "install", "-y", "x11-xserver-utils"]]
