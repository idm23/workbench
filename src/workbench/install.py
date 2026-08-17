"""Install Workbench: database, systemd service, and a health check.

Run via ./install.sh, which installs uv and then hands off here. Written in
Python rather than shell so that it can import workbench.config — the port and
database path come from the same place the running app reads them, rather than
being repeated in a second language where they can drift.

Idempotent: every step checks before acting, so re-running is a no-op.
"""

import logging
import os
import pwd
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config

from workbench.config import (
    deploy_branch,
    deploy_unit_name,
    ensure_data_dir,
    host,
    instance,
    port,
    repo_root,
    service_name,
)
from workbench.logs import BOLD, RED, YELLOW, configure_console_logging, paint

logger = logging.getLogger(__name__)

SYSTEMD_DIR = Path("/etc/systemd/system")
TEMPLATE_DIR = Path("deploy")
HEALTH_TIMEOUT_SECONDS = 20.0

#: How often the deployer checks for new commits. Not configurable through the
#: environment: it is baked into the timer at install time, and changing it
#: means re-rendering the unit anyway.
DEPLOY_INTERVAL = "5min"


def units() -> tuple[tuple[str, str], ...]:
    """Every unit this instance installs, as (unit filename, template name).

    Computed rather than constant because the names carry the instance: a
    staging install writes `workbench-staging.service` alongside production's
    `workbench.service`, and the two must never resolve to the same file.
    """
    return (
        (f"{service_name()}.service", "workbench.service.template"),
        (f"{deploy_unit_name()}.service", "workbench-deploy.service.template"),
        (f"{deploy_unit_name()}.timer", "workbench-deploy.timer.template"),
    )


def step(message: str) -> None:
    logger.info("\n%s", paint(BOLD, f"==> {message}"))


def info(message: str) -> None:
    logger.info("    %s", message)


def warn(message: str) -> None:
    logger.warning("    %s %s", paint(YELLOW, "warning:"), message)


class InstallError(Exception):
    """Something went wrong that the user needs to act on."""


def run(argv: list[str], *, privileged: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a command, escalating with sudo only when asked and only if needed."""
    if privileged and os.geteuid() != 0:
        if shutil.which("sudo") is None:
            raise InstallError(f"need root to run {' '.join(argv)}, and sudo is not installed")
        argv = ["sudo", *argv]

    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise InstallError(f"`{' '.join(argv)}` failed:\n       {detail}")
    return result


def check_not_running_under_sudo() -> None:
    """Refuse `sudo ./install.sh`.

    Running the whole install as root leaves the virtualenv root-owned and the
    service running as root — subtly wrong rather than obviously broken. Being
    genuinely root (a container, with no other user) is fine.
    """
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        raise InstallError(
            "Run this as your normal user, not with sudo:  ./install.sh\n"
            "       It escalates on its own when installing the service."
        )


def os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    if not path.is_file():
        return {}
    fields = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value.strip().strip('"')
    return fields


def check_prerequisites() -> None:
    fields = os_release()
    if fields:
        info(f"OS: {fields.get('PRETTY_NAME', 'unknown')}")
        if fields.get("ID") != "ubuntu":
            warn("built and tested on Ubuntu; other distributions are untried")

    for tool in ("curl", "git"):
        if shutil.which(tool) is None:
            raise InstallError(f"'{tool}' is required but not installed.")
    info("curl and git present")

    check_not_under_private_tmp()


#: Directories systemd replaces with a private namespace under PrivateTmp=yes.
PRIVATE_TMP_ROOTS = (Path("/tmp"), Path("/var/tmp"))


def check_not_under_private_tmp() -> None:
    """Refuse to install from a checkout the service could never see.

    The unit sets PrivateTmp=yes, which gives it its own /tmp and /var/tmp. A
    checkout under either is simply absent from inside the service, so systemd
    fails to start it — reporting only that "the control process exited with
    error code", with an empty journal because the process never got far enough
    to log anything. Diagnosing that from scratch is genuinely unpleasant, and
    it is entirely avoidable here.
    """
    root = repo_root()
    for private in PRIVATE_TMP_ROOTS:
        if root == private or private in root.parents:
            raise InstallError(
                f"the checkout is under {private}, which the service cannot see.\n"
                f"       The unit runs with PrivateTmp=yes, so {private} inside the\n"
                "       service is a private, empty directory — systemd would fail to\n"
                "       start it with no useful message. Clone somewhere under your\n"
                "       home directory instead."
            )


def apply_migrations() -> None:
    """Bring the database to head.

    Uses Alembic's Python API rather than shelling out, so failures arrive as
    real tracebacks and the alembic CLI need not be on PATH.
    """
    ensure_data_dir()
    config = Config(str(repo_root() / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root() / "alembic"))
    # env.py would otherwise call fileConfig, which disables the handlers
    # configured above. This attribute is Alembic's documented hook for callers
    # that drive it programmatically and own their own logging.
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


def service_user() -> str:
    """Who the service runs as: whoever owns the checkout.

    Derived from the repo's owner rather than from `os.geteuid()`, because this
    is also called by the automatic deployer, which runs as root in order to
    restart the unit. Reading the effective uid there would silently re-render
    the unit with `User=root` and hand an agent a root shell on the next
    deploy. The checkout's owner is the same answer in the interactive case and
    the right one in both.
    """
    return pwd.getpwuid(repo_root().stat().st_uid).pw_name


def systemd_is_running() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()


def render_unit(template_name: str) -> str:
    template = (repo_root() / TEMPLATE_DIR / template_name).read_text()
    replacements = {
        "__REPO__": str(repo_root()),
        "__USER__": service_user(),
        "__HOST__": host(),
        "__PORT__": str(port()),
        "__BRANCH__": deploy_branch(),
        "__INTERVAL__": DEPLOY_INTERVAL,
        "__INSTANCE__": instance(),
        "__SERVICE__": service_name(),
        # A staging install restores production's database before migrating;
        # production leaves this empty and never restores anything.
        "__RESTORE_FROM__": os.environ.get("WORKBENCH_RESTORE_FROM", "").strip(),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def install_units() -> set[str]:
    """Write every unit whose rendered content has changed. Returns which did.

    Separate from starting anything, because the deployer calls this too: a
    change to a unit template has to reach systemd on the next deploy, or it
    sits in the repository and only surfaces much later as a permission error
    inside an agent run.
    """
    changed: set[str] = set()

    for unit_name, template_name in units():
        rendered = render_unit(template_name)
        target = SYSTEMD_DIR / unit_name

        if target.is_file() and target.read_text() == rendered:
            continue

        # Staged inside data/ then copied with privilege, so the unprivileged
        # side never needs write access to /etc/systemd/system.
        staged = repo_root() / "data" / f"{unit_name}.staged"
        staged.write_text(rendered)
        try:
            run(["cp", str(staged), str(target)], privileged=True)
            info(f"wrote {target}")
            changed.add(unit_name)
        finally:
            staged.unlink(missing_ok=True)

    if not changed:
        info("units already up to date")
        return changed

    run(["systemctl", "daemon-reload"], privileged=True)

    # A timer re-reads its schedule when restarted, not on daemon-reload alone,
    # so a changed interval would otherwise not take effect until the next
    # reboot. The service units are left alone here — restarting those is the
    # caller's decision, and for workbench.service it is the deploy's last step.
    timer = f"{deploy_unit_name()}.timer"
    if timer in changed:
        run(["systemctl", "restart", timer], privileged=True)
        info(f"restarted {timer} to pick up its new schedule")

    return changed


def install_service() -> None:
    install_units()

    run(["systemctl", "enable", "--quiet", service_name()], privileged=True)
    run(["systemctl", "restart", service_name()], privileged=True)
    info(f"service enabled and started as user '{service_user()}'")

    # Enabling the timer is what makes a merge to the deploy branch reach this
    # machine without anyone logging in.
    run(["systemctl", "enable", "--now", "--quiet", f"{deploy_unit_name()}.timer"], privileged=True)
    info(f"deploying from '{deploy_branch()}' automatically, every {DEPLOY_INTERVAL}")


def health_check(timeout: float = HEALTH_TIMEOUT_SECONDS) -> str | None:
    """Wait for the service to answer. Returns an error message, or None.

    A result rather than an exception because the deployer calls this too, and
    that module reports failures into the journal instead of raising. The
    installer turns None-or-message back into its own error below.
    """
    url = f"http://{host()}:{port()}/healthz"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return None
        except httpx.HTTPError:
            pass
        time.sleep(0.5)

    return (
        f"the service did not answer {url} within {timeout:.0f}s.\n"
        f"       Check:  journalctl -u {service_name()} -n 50 --no-pager"
    )


def wait_for_health() -> None:
    error = health_check()
    if error is not None:
        raise InstallError(error)
    info("healthy")


def report_success() -> None:
    logger.info("\n%s", paint(BOLD, f"Workbench is running at http://{host()}:{port()}"))
    logger.info(
        "%s",
        f"""
Merges to '{deploy_branch()}' now deploy themselves, within {DEPLOY_INTERVAL}. Nothing
needs running here again — the timer fetches, migrates, and restarts.

    systemctl list-timers {deploy_unit_name()}.timer     # when the next check lands
    sudo systemctl start {deploy_unit_name()}            # deploy right now
    journalctl -u {deploy_unit_name()} -n 50 --no-pager  # what the last one did
    sudo systemctl disable --now {deploy_unit_name()}.timer   # stop deploying

To reach it from a phone over Tailscale (optional, and not automated because
it needs a browser login to your own tailnet):

    sudo tailscale set --operator=$USER   # once, if you have not already
    tailscale serve --bg {port()}

Useful commands:

    systemctl status {service_name()}
    journalctl -u {service_name()} -f
""",
    )


def main() -> int:
    configure_console_logging()
    os.chdir(repo_root())

    try:
        check_not_running_under_sudo()

        step("Checking prerequisites")
        check_prerequisites()

        step("Preparing the database")
        # uv run already built .venv from uv.lock before this module was
        # imported, so there is no separate dependency step here.
        info("dependencies installed from uv.lock")
        apply_migrations()
        info("migrations applied")

        step("Installing the service")
        if not systemd_is_running():
            warn("systemd is not running here (normal inside a container).")
            info("Skipping the service. Start Workbench manually with:")
            info(f"    .venv/bin/uvicorn workbench.app:app --host {host()} --port {port()}")
            logger.info("\n%s\n", paint(BOLD, "Install complete (without the service)."))
            return 0

        install_service()

        step("Waiting for the service to answer")
        wait_for_health()
        report_success()
        return 0

    except InstallError as error:
        logger.error("\n%s %s\n", paint(RED, "error:"), error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
