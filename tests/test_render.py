"""The render surface a gaming node gives Steam and Sunshine to capture from.

Everything here is a step that talks to the machine — Xorg, a systemd system
unit — so what can be tested is the shape of what it would do and what it does
when the machine cannot oblige, the same discipline `test_install_node.py`
already holds itself to for the model server.
"""

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
