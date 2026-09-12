"""The render surface a gaming node gives Steam and Sunshine to capture from.

Everything here is a step that talks to the machine — Xorg, a systemd `--user`
unit, `loginctl` — so what can be tested is the shape of what it would do and
what it does when the machine cannot oblige, the same discipline
`test_install_node.py` already holds itself to for the model server.
"""

import pytest

from workbench import render
from workbench.config import GAMESCOPE, X11_DUMMY


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


def test_no_gaming_user_skips_x11_rather_than_failing(monkeypatch, caplog):
    monkeypatch.setattr(render, "systemd_is_running", lambda: True)
    monkeypatch.setattr(render, "gaming_user", lambda: None)

    with caplog.at_level("WARNING"):
        assert render._install_x11_dummy() is False

    assert "no gaming user recorded" in caplog.text


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


def test_linger_is_only_enabled_once(monkeypatch):
    """`loginctl enable-linger` is idempotent on the machine, but a check first
    means one fewer privileged call on every re-run of the installer."""
    calls = []
    monkeypatch.setattr(render, "_linger_enabled", lambda player: False)
    monkeypatch.setattr(render, "run", lambda argv, **k: calls.append(argv))

    render._ensure_linger("ian")

    assert calls == [["loginctl", "enable-linger", "ian"]]


def test_linger_already_enabled_is_left_alone(monkeypatch):
    monkeypatch.setattr(render, "_linger_enabled", lambda player: True)
    monkeypatch.setattr(
        render, "run", lambda *a, **k: pytest.fail("enable-linger should not have been called")
    )

    render._ensure_linger("ian")  # must not raise


def test_render_session_is_unknown_with_no_gaming_user(monkeypatch):
    monkeypatch.setattr(render, "gaming_user", lambda: None)
    assert render.render_session_is_up() is None


def test_render_session_is_false_for_the_unimplemented_gamescope_backend(monkeypatch):
    monkeypatch.setattr(render, "gaming_user", lambda: "ian")
    monkeypatch.setattr(render, "render_backend", lambda: GAMESCOPE)
    assert render.render_session_is_up() is False


def test_render_session_is_unknown_for_x11_dummy_today(monkeypatch):
    """See render_session_is_up's own docstring: no real probe exists yet."""
    monkeypatch.setattr(render, "gaming_user", lambda: "ian")
    monkeypatch.setattr(render, "render_backend", lambda: X11_DUMMY)
    assert render.render_session_is_up() is None
