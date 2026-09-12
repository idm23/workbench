"""Give Steam and Sunshine something to render into, on a machine with nothing
plugged into its GPU.

`install_gaming()` gets this far and then has nowhere to put a game: no
desktop environment, no display manager, no monitor. Two real answers exist
for a headless NVIDIA box, and neither can be picked from documentation alone:

- **X11 + a virtual display + NvFBC capture** — the deeper, longer-running
  community precedent specifically for *Sunshine on NVIDIA*, not NVIDIA in
  general. Older stack, better-trodden path for this exact pairing.
- **`gamescope` (a Wayland micro-compositor) + KMS/DRM-lease capture** — newer,
  more Steam-Deck-shaped than server-shaped. NVIDIA's headless-Wayland output
  story has historically been the less-exercised one on that vendor's driver;
  whether that is still true on whatever driver a given node has is a question
  for that node, not for this docstring.

This module exists so that question stays contained. `install()` is the one
call site `install_gaming()` uses; everything about *which* backend and *how*
it works lives behind it, keyed off `config.render_backend()`. Switching a
node from one to the other later is writing `_install_gamescope()` for real
and flipping the marker — not touching `gaming.py`, the doctor, the polkit
rule, or `install_gaming()`'s call site.

`x11-dummy` is the default, for the precedent reason above, and has now run
against real hardware. The one thing that did not survive contact with it was
the *unit shape*, not the display recipe: a first attempt ran the X server as
a `systemd --user` unit for the gaming account, on the theory that Steam and
Sunshine's own privilege narrowing should extend to the display too. It
cannot work, independent of any one machine's configuration — a user unit
always runs as whoever's own systemd instance loaded it, so it can never be
root, and opening a VT is a root-only operation on a machine with no setuid
`Xorg.wrap` (confirmed on this project's own node: `/dev/tty7` is
`crw-------`, zero permission bits for anyone else). `workbench-x11.service`
is a system unit, root, for that reason alone — see its template's own
comment for the rest of the story. Steam and Sunshine stay unprivileged: they
are X clients, not the server, and that split needs nothing this module owns.
"""

import logging
import shutil
import subprocess
from pathlib import Path

from workbench.config import GAMESCOPE, gaming_user, render_backend
from workbench.install import (
    SYSTEMD_DIR,
    InstallError,
    info,
    render_unit,
    run,
    systemd_is_running,
    warn,
    write_privileged,
)

logger = logging.getLogger(__name__)

#: Where the X11-dummy backend's static configuration lands. Not instance-
#: scoped like the systemd units: a machine plays at most one game at a time
#: regardless of how many Workbench instances happen to be installed on it.
X11_CONF_PATH = Path("/etc/X11/xorg.conf.d/10-workbench-dummy.conf")

X11_UNIT_NAME = "workbench-x11.service"


def install() -> bool:
    """Set up whatever this node's declared render backend needs.

    Returns whether a render surface now exists. `False` is an ordinary
    answer, not a failure — mirrors `install_inference_server()`'s shape for
    the same reason: the fresh-install-in-a-container CI path has no systemd
    and no gaming user, and the install still has to finish and say why it
    skipped this rather than raise.
    """
    backend = render_backend()
    if backend == GAMESCOPE:
        return _install_gamescope()
    return _install_x11_dummy()


def _ensure_xorg_installed() -> bool:
    """Whether the Xorg server binary is present, installing it if not.

    Not assumed: a minimal Ubuntu Server image has no display server at all,
    unlike the NVIDIA driver itself, which a gaming node already has by the
    time `install_gaming()` runs.
    """
    if shutil.which("Xorg") is not None:
        return True
    info("installing xserver-xorg-core")
    try:
        run(["apt-get", "install", "-y", "xserver-xorg-core"], privileged=True, stream=True)
    except InstallError as error:
        warn(f"could not install xserver-xorg-core: {error}")
        return False
    return True


def _write_xorg_conf() -> None:
    rendered = render_unit("xorg-dummy.conf.template")
    if X11_CONF_PATH.is_file() and X11_CONF_PATH.read_text() == rendered:
        info("Xorg configuration already up to date")
        return
    X11_CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_privileged(X11_CONF_PATH, rendered, staged_as="xorg-dummy.conf.staged")
    info(f"wrote {X11_CONF_PATH}")


def _write_x11_unit() -> bool:
    """Render the system unit that runs the X server. Returns whether it changed."""
    rendered = render_unit("workbench-x11.service.template")
    target = SYSTEMD_DIR / X11_UNIT_NAME
    if target.is_file() and target.read_text() == rendered:
        info(f"{X11_UNIT_NAME} already up to date")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    write_privileged(target, rendered, staged_as=f"{X11_UNIT_NAME}.staged")
    info(f"wrote {target}")
    return True


def _enable_system_unit() -> bool:
    """Start the render surface, root, no different from any other system unit.

    No per-account impersonation needed — see this module's own docstring for
    why that was tried first and does not apply here.
    """
    try:
        run(["systemctl", "enable", "--now", X11_UNIT_NAME], privileged=True)
    except InstallError as error:
        warn(f"could not enable {X11_UNIT_NAME}: {error}")
        return False
    info(f"{X11_UNIT_NAME} enabled")
    return True


def _install_x11_dummy() -> bool:
    if not systemd_is_running():
        warn("no systemd here, so no render surface was installed.")
        info("A real gaming node would get an X server standing in for a monitor.")
        return False

    if not _ensure_xorg_installed():
        warn("could not install xserver-xorg-core; no render surface was installed.")
        return False

    _write_xorg_conf()
    changed = _write_x11_unit()
    if changed:
        run(["systemctl", "daemon-reload"], privileged=True)

    return _enable_system_unit()


def _install_gamescope() -> bool:
    """The Wayland alternative, not yet built.

    Named and stubbed so the dispatch point and the doctor check already have
    the right shape to grow into if `x11-dummy` proves unreliable on some
    node's card — see this module's own docstring for why it is the fallback
    rather than the default.
    """
    warn("the gamescope render backend is not implemented yet.")
    info("Set WORKBENCH_RENDER_BACKEND=x11-dummy, or re-run without --capabilities=gaming.")
    return False


def render_session_is_up() -> bool | None:
    """Whether a render surface is actually running right now, for the doctor.

    A real probe now that `workbench-x11.service` is a system unit: `systemctl
    is-active` needs no per-account impersonation, unlike the `systemd --user`
    shape this was first tried against. `None` only when there is nobody
    playing at all, or nothing installed to ask about (`gamescope`, still
    unimplemented) — a question this doctor check should not even be asking
    yet, rather than a probe that failed.
    """
    if not gaming_user():
        return None
    if render_backend() == GAMESCOPE:
        # Nothing to probe: the backend itself is not implemented yet.
        return False
    # subprocess directly, not install.run(): `systemctl is-active` returns
    # nonzero for every state that is not "active", by design, and run()
    # would turn that ordinary "no" into a raised InstallError.
    probe = subprocess.run(
        ["systemctl", "is-active", X11_UNIT_NAME], capture_output=True, text=True, check=False
    )
    return probe.stdout.strip() == "active"
