"""Joining the home tailnet, and asking a Tailscale daemon what it thinks it is.

Shared by every install and by the doctor, so it imports nothing but the
standard library: the installers run before a virtualenv exists.

**Joining can now be unattended, and only when someone hands over a key.**
CLAUDE.md used to say joining a tailnet is a browser step no script can take,
and without a key that is still true — the doctor names the login command
exactly as before. With a tagged, pre-approved auth key it is not: the machine
joins on the install's own word, and because it is *tagged* it belongs to the
tailnet rather than to a person, which is what turns its key expiry off. That
is the half that matters. A device logged in as a person expires in 180 days
and asks for the browser again, on every machine, forever.

**Every home machine runs Tailscale SSH.** It is what makes `ssh homebox-node-2`
work from any device on the tailnet without distributing keys, and it was
already on — turned on by hand, which is the thing this project exists not to
have. Whether it asks for a browser re-check is decided by the tailnet policy
(`"action": "check"` against `"accept"`), not by any machine; see
`docs/nodes.md`.

The key is never logged, never written to disk, and never put in a marker. It
is spent once, by `tailscale up`, and a re-install that does not mention one
leaves a joined machine exactly as it was.
"""

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: The tag a machine joined by an install advertises. It must be listed under
#: `tagOwners` in the tailnet policy, or `tailscale up` refuses it.
DEFAULT_TAG = "tag:workbench"

#: The environment spelling of `--tailscale-auth-key`. Forwarded through sudo
#: like every other `WORKBENCH_*` variable.
AUTH_KEY_SETTING = "WORKBENCH_TAILSCALE_AUTH_KEY"

#: Vendor installer, for a machine given a key and no Tailscale. Same reasoning
#: as Ollama's: its package repository setup per distribution is theirs to get
#: right, not ours.
TAILSCALE_INSTALLER = "https://tailscale.com/install.sh"

STATUS_TIMEOUT_SECONDS = 10
UP_TIMEOUT_SECONDS = 120


# --- The external machine's second daemon ------------------------------------

#: The user unit an external machine runs for the home tailnet. Kept as the name
#: the first one (on a work laptop) was given by hand, so installing over it
#: converges rather than starting a second daemon beside it.
EXTERNAL_UNIT = "tailscaled-home.service"

#: A port of its own. The system daemon on the *other* tailnet already has
#: 41641, and two daemons on one UDP port means neither gets a direct path.
EXTERNAL_PORT = 41642


def external_socket() -> Path:
    """Where the external machine's home-tailnet daemon listens.

    Under the user's runtime directory, matching `%t` in the unit, so only that
    user can talk to it — which is the whole of its access control.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "tailscale-home" / "tailscaled.sock"


def external_unit() -> str:
    """The user unit, as text.

    Userspace networking is the point: it creates no interface and touches no
    routes or DNS, so the *other* tailnet the system daemon is on is left exactly
    as its owner configured it, and only programs pointed at this socket can
    reach home. On a work laptop that is the property that makes this
    acceptable at all.
    """
    return f"""\
# Written by ./install.sh --role=external. Re-running it rewrites this file.
#
# A second Tailscale daemon, logged in to the home tailnet, alongside whatever
# the system one is on. Userspace networking creates no interface and touches no
# routes or DNS, so only programs pointed at its socket can reach the home tailnet.
[Unit]
Description=Tailscale daemon for the home tailnet (userspace)
Documentation=https://tailscale.com/docs/
After=network.target

[Service]
Type=notify
ExecStart=/usr/sbin/tailscaled --tun=userspace-networking \\
    --state=%S/tailscale-home/tailscaled.state \\
    --socket=%t/tailscale-home/tailscaled.sock \\
    --port={EXTERNAL_PORT}
StateDirectory=tailscale-home
StateDirectoryMode=0700
RuntimeDirectory=tailscale-home
RuntimeDirectoryMode=0700
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def external_hostname() -> str:
    """What the external machine is called on the home tailnet.

    Suffixed, because the same laptop may already be on the home tailnet under
    its own name from before it moved to the other one, and two devices with one
    name get `-1` appended by the coordination server instead.
    """
    return f"{os.uname().nodename}-home"


# --- Reading a daemon --------------------------------------------------------


@dataclass(frozen=True)
class TailnetStatus:
    """What `tailscale status --json` said, narrowed to the questions asked."""

    #: `Running`, `NeedsLogin`, `NeedsMachineAuth`, `Stopped`, `Starting`.
    backend_state: str
    hostname: str
    tailnet: str | None
    tags: tuple[str, ...]

    #: None when the key never expires — tagged, or disabled in the console.
    key_expiry: datetime | None

    #: Peer hostname -> online.
    peers: dict[str, bool]

    @property
    def running(self) -> bool:
        return self.backend_state == "Running"


def cli(socket: Path | None) -> list[str]:
    """The `tailscale` argv prefix for one daemon."""
    return ["tailscale", *([f"--socket={socket}"] if socket else [])]


def read_status(socket: Path | None = None) -> TailnetStatus | str:
    """Ask a daemon what it is. A string is why it could not say.

    `status --json` exits non-zero when logged out while still printing a
    usable document, so the verdict comes from the output.
    """
    try:
        probe = subprocess.run(
            [*cli(socket), "status", "--json"],
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return "tailscale is not installed."
    except (OSError, subprocess.SubprocessError) as error:
        return f"tailscale could not be asked: {error}"

    try:
        document = json.loads(probe.stdout or "")
    except json.JSONDecodeError:
        detail = (probe.stderr or probe.stdout or "").strip().splitlines()
        return detail[0] if detail else "tailscale answered with nothing readable."
    return parse_status(document)


def parse_status(document: dict) -> TailnetStatus:
    me = document.get("Self") or {}
    tailnet = (document.get("CurrentTailnet") or {}).get("Name")
    peers = {
        str(peer.get("HostName", "")): bool(peer.get("Online"))
        for peer in (document.get("Peer") or {}).values()
    }
    return TailnetStatus(
        backend_state=str(document.get("BackendState", "")),
        hostname=str(me.get("HostName", "")),
        tailnet=tailnet,
        tags=tuple(me.get("Tags") or ()),
        key_expiry=_parse_time(me.get("KeyExpiry")),
        peers=peers,
    )


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw or raw.startswith("0001-"):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- Joining -------------------------------------------------------------------


def _argument(name: str) -> str | None:
    """`--name=value` or `--name value`, hand-parsed like the installers' others."""
    argv = sys.argv[1:]
    for index, argument in enumerate(argv):
        if argument.startswith(f"{name}="):
            return argument.split("=", 1)[1].strip() or None
        if argument == name and index + 1 < len(argv):
            return argv[index + 1].strip() or None
    return None


def auth_key() -> str | None:
    """The key this install was handed, from the flag or the environment."""
    return _argument("--tailscale-auth-key") or os.environ.get(AUTH_KEY_SETTING, "").strip() or None


def tag() -> str:
    """The tag to join with. Spelled with or without the `tag:` prefix."""
    chosen = _argument("--tailscale-tag") or DEFAULT_TAG
    return chosen if chosen.startswith("tag:") else f"tag:{chosen}"


def up_command(
    key: str, *, socket: Path | None = None, hostname: str | None = None, ssh: bool = True
) -> list[str]:
    """The `tailscale up` that joins with a key. The key is the one secret in it."""
    return [
        *cli(socket),
        "up",
        f"--auth-key={key}",
        f"--advertise-tags={tag()}",
        *(["--ssh"] if ssh else []),
        *([f"--hostname={hostname}"] if hostname else []),
    ]


def login_command(
    *, socket: Path | None = None, hostname: str | None = None, ssh: bool = True
) -> str:
    """The browser-login command, for a machine given no key. Shown, never run."""
    prefix = "" if socket else "sudo "
    argv = [
        *cli(socket),
        "up",
        *(["--ssh"] if ssh else []),
        *([f"--hostname={hostname}"] if hostname else []),
    ]
    return prefix + " ".join(argv)


@dataclass(frozen=True)
class Joined:
    status: TailnetStatus


@dataclass(frozen=True)
class NotJoined:
    #: Why, in a sentence a person reads in the install log.
    reason: str


def join(
    key: str, *, socket: Path | None = None, hostname: str | None = None, ssh: bool = True
) -> Joined | NotJoined:
    """Spend the key. Never raises, never echoes the key back."""
    try:
        result = subprocess.run(
            up_command(key, socket=socket, hostname=hostname, ssh=ssh),
            capture_output=True,
            text=True,
            timeout=UP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return NotJoined(f"tailscale up could not be run: {error}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").replace(key, "<key>").strip()
        return NotJoined(f"tailscale up refused the key: {detail}")
    status = read_status(socket)
    if isinstance(status, str) or not status.running:
        return NotJoined("tailscale up succeeded but the daemon is not running.")
    return Joined(status)
