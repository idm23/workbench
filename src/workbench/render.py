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

`x11-dummy` is the default, for the precedent reason above. It has not run
against real hardware yet — see the `TODO` markers in `deploy/xorg-dummy.conf.
template` and `deploy/workbench-x11.service.template` for exactly which parts
of the recipe are documented-but-unverified rather than proven on this
project's actual node.
"""

import logging
import pwd
import shutil
from pathlib import Path

from workbench.config import GAMESCOPE, gaming_user, render_backend
from workbench.install import (
    info,
    render_unit,
    run,
    run_as_account,
    systemd_is_running,
    warn,
    write_privileged,
)

logger = logging.getLogger(__name__)

#: Where the X11-dummy backend's static configuration lands. Not instance-
#: scoped like the systemd units: a machine plays at most one game at a time
#: regardless of how many Workbench instances happen to be installed on it.
X11_CONF_PATH = Path("/etc/X11/xorg.conf.d/10-workbench-dummy.conf")

#: System-wide *user* unit directory — distinct from `install.SYSTEMD_DIR`,
#: which is system units. A user unit implicitly runs as whoever's
#: `systemd --user` instance loaded it; there is no `User=` to set the way
#: there is for `workbench-gaming.service`.
USER_UNIT_DIR = Path("/etc/systemd/user")

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
    result = run(["apt-get", "install", "-y", "xserver-xorg-core"], privileged=True, stream=True)
    return result.returncode == 0


def _write_xorg_conf() -> None:
    rendered = render_unit("xorg-dummy.conf.template")
    if X11_CONF_PATH.is_file() and X11_CONF_PATH.read_text() == rendered:
        info("Xorg configuration already up to date")
        return
    X11_CONF_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_privileged(X11_CONF_PATH, rendered, staged_as="xorg-dummy.conf.staged")
    info(f"wrote {X11_CONF_PATH}")


def _write_x11_unit() -> bool:
    """Render the user unit that runs the X server. Returns whether it changed."""
    rendered = render_unit("workbench-x11.service.template")
    target = USER_UNIT_DIR / X11_UNIT_NAME
    if target.is_file() and target.read_text() == rendered:
        info(f"{X11_UNIT_NAME} already up to date")
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    write_privileged(target, rendered, staged_as=f"{X11_UNIT_NAME}.staged")
    info(f"wrote {target}")
    return True


def _linger_enabled(player: str) -> bool:
    probe = run(["loginctl", "show-user", player, "--property=Linger", "--value"], privileged=True)
    return probe.stdout.strip() == "yes"


def _ensure_linger(player: str) -> None:
    """Let `player`'s `systemd --user` instance run with nobody logged in.

    Without this, the user unit below exists but never starts: a `--user`
    manager is normally only alive for the duration of a login session, and
    this account may never have one.
    """
    if _linger_enabled(player):
        info(f"linger already enabled for '{player}'")
        return
    run(["loginctl", "enable-linger", player], privileged=True)
    info(
        f"enabled linger for '{player}', so their session survives logging out (or not logging in)"
    )


def _enable_user_unit(player: str) -> bool:
    """Start the render surface as `player`, not as root.

    A root process cannot toggle another account's `systemctl --user` units
    directly — this is `install.run_as_account` (see its own docstring) doing
    the same setuid trick `service_run` uses, aimed at a different account.

    TODO: verify against real hardware. `XDG_RUNTIME_DIR` is threaded through
    by hand because setuid alone does not set it the way a real login's
    `pam_systemd` would; whether that is sufficient for `systemctl --user` to
    reach the right bus, with linger enabled but no active session, is exactly
    the part of this recipe that only running it on the actual box confirms.
    """
    account = pwd.getpwnam(player)
    extra_env = {"XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}"}
    result = run_as_account(
        ["systemctl", "--user", "enable", "--now", X11_UNIT_NAME],
        account,
        extra_env=extra_env,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        warn(f"could not enable {X11_UNIT_NAME} for '{player}': {detail}")
        return False
    info(f"{X11_UNIT_NAME} enabled for '{player}'")
    return True


def _install_x11_dummy() -> bool:
    if not systemd_is_running():
        warn("no systemd here, so no render surface was installed.")
        info("A real gaming node would get an X server standing in for a monitor.")
        return False

    player = gaming_user()
    if not player:
        warn("no gaming user recorded, so there is nobody to run a render surface for.")
        info("Re-run with:  --capabilities=inference,gaming --gaming-user=<the person>")
        return False

    if not _ensure_xorg_installed():
        warn("could not install xserver-xorg-core; no render surface was installed.")
        return False

    _write_xorg_conf()
    changed = _write_x11_unit()
    if changed:
        run(["systemctl", "daemon-reload"], privileged=True)

    _ensure_linger(player)
    return _enable_user_unit(player)


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

    `None` when this can't be answered at all. `systemctl --user -M <user>@`
    (the "machine" spec for reaching another account's user-manager instance
    without becoming that account) is the documented way to ask this, but it
    has not been exercised against a real gaming node yet —

    TODO: verify against real hardware, then replace this with an actual
    probe. Until then, `UNKNOWN` is the honest answer: it is worth more to the
    doctor than a `FAIL` (or `OK`) that might simply be wrong.
    """
    if not gaming_user():
        return None
    if render_backend() == GAMESCOPE:
        # Nothing to probe: the backend itself is not implemented yet.
        return False
    return None
