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

from workbench.config import ensure_data_dir, host, port, repo_root
from workbench.logs import BOLD, RED, YELLOW, configure_console_logging, paint

logger = logging.getLogger(__name__)

SERVICE_NAME = "workbench"
UNIT_PATH = Path("/etc/systemd/system") / f"{SERVICE_NAME}.service"
TEMPLATE_PATH = Path("deploy") / "workbench.service.template"
HEALTH_TIMEOUT_SECONDS = 20.0


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
    return pwd.getpwuid(os.geteuid()).pw_name


def systemd_is_running() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()


def render_unit() -> str:
    template = (repo_root() / TEMPLATE_PATH).read_text()
    replacements = {
        "__REPO__": str(repo_root()),
        "__USER__": service_user(),
        "__HOST__": host(),
        "__PORT__": str(port()),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def install_service() -> None:
    rendered = render_unit()

    current = UNIT_PATH.read_text() if UNIT_PATH.is_file() else None
    if current == rendered:
        info("unit already up to date")
    else:
        staged = repo_root() / "data" / f"{SERVICE_NAME}.service.staged"
        staged.write_text(rendered)
        try:
            run(["cp", str(staged), str(UNIT_PATH)], privileged=True)
            run(["systemctl", "daemon-reload"], privileged=True)
            info(f"wrote {UNIT_PATH}")
        finally:
            staged.unlink(missing_ok=True)

    run(["systemctl", "enable", "--quiet", SERVICE_NAME], privileged=True)
    run(["systemctl", "restart", SERVICE_NAME], privileged=True)
    info(f"service enabled and started as user '{service_user()}'")


def wait_for_health() -> None:
    url = f"http://{host()}:{port()}/healthz"
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                info("healthy")
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)

    raise InstallError(
        f"the service did not answer {url} within {HEALTH_TIMEOUT_SECONDS:.0f}s.\n"
        f"       Check:  journalctl -u {SERVICE_NAME} -n 50 --no-pager"
    )


def report_success() -> None:
    """Print what works now, and the steps a script cannot take for you.

    Everything below needs either a browser login or a secret this machine
    cannot derive. Printing them here is the compromise that keeps `install.sh`
    the only command while being honest that agent runs need two more.
    """
    logger.info("\n%s", paint(BOLD, f"Workbench is running at http://{host()}:{port()}"))
    logger.info(
        "%s",
        f"""
Tasks work now. Running them with an agent needs two credentials, neither of
which can go in this script.

1. Sign in to Claude, as the user the service runs as:

       claude

   Agent runs bill against that account. Without it, starting a run fails with
   an authentication error.

2. Give it push access, so finished runs can open pull requests:

       sudo install -d -m 755 /etc/workbench
       sudo touch /etc/workbench/env && sudo chmod 600 /etc/workbench/env
       # then add:  WORKBENCH_GITHUB_TOKEN=github_pat_...

   A fine-grained token, limited to the repositories you want touched, with
   contents:write and pull_requests:write. Without it tasks and planning still
   work; only pushing and opening pull requests do not.

   Then:  sudo systemctl restart {SERVICE_NAME}

To reach it from a phone over Tailscale (optional, and not automated because
it needs a browser login to your own tailnet):

    sudo tailscale set --operator=$USER   # once, if you have not already
    tailscale serve --bg {port()}

Useful commands:

    systemctl status {SERVICE_NAME}
    journalctl -u {SERVICE_NAME} -f
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
