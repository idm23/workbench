"""Install an external machine: one that lives elsewhere and only needs to reach home.

A work laptop is the first and the reason. It is on its employer's tailnet,
which it must stay on, and it needs to reach the head and the nodes on the home
one. Before this existed that took a hand-written user unit and a hand-written
block in `~/.ssh/config`, which is exactly the kind of setup that is lost with
the laptop.

What it is, in the same terms as the other two roles:

- **It runs as the person, never as root.** Everything it installs is theirs: a
  systemd *user* unit, a block in their own SSH config. Run under sudo, it would
  put a daemon in root's user manager and a route in root's SSH config, both of
  which work and neither of which the person's own `ssh` ever reads.
- **It touches nothing on the other tailnet.** The home daemon uses userspace
  networking: no interface, no routes, no DNS. Only programs pointed at its
  socket reach home, which is what makes this acceptable on a machine somebody
  else administers.
- **No deployment, no service account, no deploy timer.** The checkout is
  usually a developer's own clone, and a timer fast-forwarding someone's working
  tree is the opposite of help. What it installs is small enough that re-running
  `./install.sh --role=external` after a pull is the update.
- **It lends nothing, so it registers nothing.** A node heartbeats because the
  head needs to know where its GPU is. Nothing needs to know where a laptop is.

The one step that may need a person is the login, and with
`--tailscale-auth-key` it does not.
"""

import fnmatch
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from workbench import tailnet
from workbench.config import ROLE_EXTERNAL, repo_root, role_marker, ssh_hosts, ssh_hosts_marker
from workbench.install import InstallError, check_invocation, info, step, warn
from workbench.logs import BOLD, RED, configure_console_logging, paint

logger = logging.getLogger(__name__)

ENTRY = "workbench.install_external"

#: How long the freshly started daemon gets to open its socket.
SOCKET_WAIT_SECONDS = 15.0

#: The fences around the block this owns in `~/.ssh/config`. Everything outside
#: them is the person's, and is never rewritten.
SSH_BLOCK_START = "# >>> workbench external (managed by ./install.sh --role=external) >>>"
SSH_BLOCK_END = "# <<< workbench external <<<"


def user_unit_path() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "systemd" / "user" / tailnet.EXTERNAL_UNIT


def ssh_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def check_not_root() -> None:
    if os.geteuid() == 0:
        raise InstallError(
            "Run this as yourself, not as root.\n"
            "       Everything it installs is yours: a systemd user unit and a block in\n"
            "       your own ~/.ssh/config. Under sudo both would land in root's, where\n"
            "       your ssh never looks.\n"
            "       Run:  ./install.sh --role=external"
        )


def check_tailscale_installed() -> None:
    missing = [tool for tool in ("tailscale", "tailscaled") if _which(tool) is None]
    if missing:
        raise InstallError(
            f"{' and '.join(missing)} not found. Install Tailscale first (this needs root once):\n"
            f"       curl -fsSL {tailnet.TAILSCALE_INSTALLER} | sh\n"
            "       The system daemon it starts can stay on whichever tailnet it is on;\n"
            "       this install adds a second one for home and leaves that one alone."
        )


def _which(tool: str) -> str | None:
    # tailscaled lives in /usr/sbin, which is not on every person's PATH.
    return shutil.which(tool) or shutil.which(tool, path="/usr/sbin:/sbin")


def _ssh_hosts_argument() -> list[str] | None:
    argv = sys.argv[1:]
    for index, argument in enumerate(argv):
        raw = None
        if argument.startswith("--ssh-hosts="):
            raw = argument.split("=", 1)[1]
        elif argument == "--ssh-hosts" and index + 1 < len(argv):
            raw = argv[index + 1]
        if raw is not None:
            return raw.replace(",", " ").split()
    return None


def record(hosts: list[str] | None) -> None:
    """Write the role and, when given, the host patterns. As the person, so no chown."""
    marker = role_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{ROLE_EXTERNAL}\n", encoding="utf-8")
    info(f"this machine is {ROLE_EXTERNAL}")
    if hosts is not None:
        ssh_hosts_marker().write_text(" ".join(hosts) + "\n", encoding="utf-8")
        info(f"routing ssh to {' '.join(hosts)} through the home tailnet")


def _systemctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments], capture_output=True, text=True, check=False
    )


def install_daemon() -> None:
    """Write the user unit if it differs, and make sure it is running.

    Restarted only when the file changed. A restart drops every connection
    riding the daemon, and a person may be running this from a terminal that is
    itself an ssh session through it.
    """
    path = user_unit_path()
    wanted = tailnet.external_unit()
    try:
        changed = path.read_text(encoding="utf-8") != wanted
    except OSError:
        changed = True

    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(wanted, encoding="utf-8")
        info(f"wrote {path}")
        _systemctl("daemon-reload")

    enabled = _systemctl("enable", tailnet.EXTERNAL_UNIT)
    if enabled.returncode != 0:
        detail = (enabled.stderr or enabled.stdout).strip()
        raise InstallError(f"could not enable {tailnet.EXTERNAL_UNIT}: {detail}")
    started = _systemctl("restart" if changed else "start", tailnet.EXTERNAL_UNIT)
    if started.returncode != 0:
        detail = (started.stderr or started.stdout).strip()
        raise InstallError(
            f"{tailnet.EXTERNAL_UNIT} would not start: {detail}\n"
            f"       journalctl --user -u {tailnet.EXTERNAL_UNIT} -n 50 --no-pager"
        )
    info(f"{tailnet.EXTERNAL_UNIT} is running")

    # Lingering keeps the daemon up with nobody logged in, which on a laptop is
    # rarely wanted and needs root to grant. Said rather than done.
    if not Path(f"/var/lib/systemd/linger/{os.environ.get('USER', '')}").exists():
        info("it stops when you log out; `loginctl enable-linger` keeps it up.")


def wait_for_daemon() -> tailnet.TailnetStatus | None:
    socket = tailnet.external_socket()
    deadline = time.monotonic() + SOCKET_WAIT_SECONDS
    while True:
        status = tailnet.read_status(socket)
        if not isinstance(status, str) and status.backend_state not in ("", "NoState", "Starting"):
            return status
        if time.monotonic() >= deadline:
            return None if isinstance(status, str) else status
        time.sleep(0.5)


def log_in(status: tailnet.TailnetStatus | None) -> None:
    socket = tailnet.external_socket()
    if status is not None and status.running:
        info(f"on the home tailnet '{status.tailnet}' as {status.hostname}")
        return

    key = tailnet.auth_key()
    if key is None:
        info("not logged in. Re-run with --tailscale-auth-key, or log in once with:")
        hostname = tailnet.external_hostname()
        info(f"    {tailnet.login_command(socket=socket, hostname=hostname, ssh=False)}")
        return

    # No Tailscale SSH here: this machine is a way in, not somewhere to be
    # reached, and serving SSH from a work laptop is not ours to decide.
    outcome = tailnet.join(key, socket=socket, hostname=tailnet.external_hostname(), ssh=False)
    if isinstance(outcome, tailnet.NotJoined):
        warn(outcome.reason)
        return
    info(f"joined '{outcome.status.tailnet}' as {outcome.status.hostname}, tagged {tailnet.tag()}")


def ssh_block(hosts: list[str]) -> str:
    binary = _which("tailscale") or "tailscale"
    return "\n".join(
        [
            SSH_BLOCK_START,
            "# Reached through the home tailnet's own daemon, so this machine can stay",
            "# on whichever tailnet its system daemon is on.",
            f"Host {' '.join(hosts)}",
            f"  ProxyCommand {binary} --socket={tailnet.external_socket()} nc %h %p",
            SSH_BLOCK_END,
        ]
    )


def with_block(existing: str, block: str) -> str:
    """`existing` with the managed block replaced, or put first if absent.

    First, because ssh takes the first value it finds for each option, and a
    later `Host *` block that sets ProxyCommand would otherwise win.
    """
    start = existing.find(SSH_BLOCK_START)
    end = existing.find(SSH_BLOCK_END)
    if start != -1 and end > start:
        return existing[:start] + block + existing[end + len(SSH_BLOCK_END) :]
    return f"{block}\n\n{existing}" if existing.strip() else f"{block}\n"


def install_ssh_route() -> None:
    hosts = ssh_hosts()
    if not hosts:
        warn("no host patterns recorded, so ssh was not routed home.")
        info("Re-run with --ssh-hosts 'homebox-*' (any ssh Host patterns).")
        return

    path = ssh_config_path()
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    updated = with_block(existing, ssh_block(hosts))
    if updated != existing:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
        path.chmod(0o600)
        info(f"updated {path}")
    else:
        info(f"{path} already routes {' '.join(hosts)} home")

    duplicates = [
        line.strip()
        for line in _outside_block(updated).splitlines()
        if line.strip().lower().startswith("host ")
        and any(fnmatch.fnmatch(word, pattern) for word in line.split()[1:] for pattern in hosts)
    ]
    if duplicates:
        listed = ", ".join(repr(line) for line in duplicates)
        warn(f"{path} also has {listed} outside the managed block;")
        info("the managed block comes first and wins, so the other can be deleted.")


def _outside_block(text: str) -> str:
    start = text.find(SSH_BLOCK_START)
    end = text.find(SSH_BLOCK_END)
    if start == -1 or end < start:
        return text
    return text[:start] + text[end + len(SSH_BLOCK_END) :]


def report_outstanding() -> bool:
    """The doctor, as the person, from this checkout. `uv run` builds its venv."""
    uv = shutil.which("uv") or "uv"
    result = subprocess.run(
        [uv, "run", "--quiet", "python", "-m", "workbench.doctor"], cwd=repo_root(), check=False
    )
    return result.returncode == 0


def main() -> int:
    configure_console_logging()
    os.chdir(repo_root())

    try:
        check_invocation()
        check_not_root()

        step("Checking prerequisites")
        check_tailscale_installed()
        info("tailscale and tailscaled present")

        step("Recording what this machine is")
        record(_ssh_hosts_argument())

        step("Starting the home tailnet's daemon")
        install_daemon()

        step("Joining the home tailnet")
        log_in(wait_for_daemon())

        step("Routing ssh home")
        install_ssh_route()

        logger.info("\n%s\n", paint(BOLD, "External install complete."))

        step("Checking what still needs a person")
        report_outstanding()
        return 0

    except InstallError as error:
        logger.error("\n%s %s\n", paint(RED, "error:"), error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
