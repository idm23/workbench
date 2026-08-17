"""Bring the running install up to date with `main`.

Invoked as `python -m workbench.deploy`, on a timer. Fetches, fast-forwards,
syncs dependencies, applies migrations, and restarts the service — so merging a
pull request is the whole deployment.

**Why polling rather than a webhook or a CI push.** The server has no public
ingress: it is reachable only over Tailscale, and `tailscale funnel` is ruled
out on purpose. GitHub therefore cannot reach in, and a GitHub Actions job
cannot SSH in either. A self-hosted runner would work but means registering a
long-lived credential and letting workflow code execute here. Polling needs no
inbound path, no secret, and no new daemon — just a systemd timer. The cost is
that a merge lands within the timer interval rather than instantly.

**Why a separate unit from the web service.** Restarting `workbench` from
inside `workbench` kills the process doing the restarting. Running under
`workbench-deploy.service` puts this in its own cgroup, so the restart it
triggers cannot kill it. Agent runs are already detached for the same reason,
so a deploy does not disturb one in flight.

Every step is a result rather than an exception, because most of the failures
here are ordinary conditions — a dirty checkout, a diverged branch, no network
— and they need to be reported into the journal, not raised into a traceback.
"""

import logging
import os
import pwd
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from workbench.config import deploy_branch, host, port, repo_root
from workbench.logs import configure_console_logging

logger = logging.getLogger(__name__)

SERVICE_NAME = "workbench"

#: Long enough for a cold `uv sync` that has to fetch the SDK's ~310MB bundled
#: binary over a home connection.
SYNC_TIMEOUT_SECONDS = 900
GIT_TIMEOUT_SECONDS = 300
RESTART_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class Deployed:
    """The install moved to a new commit."""

    revision: str


@dataclass(frozen=True)
class AlreadyCurrent:
    """Nothing to do. The overwhelmingly common outcome."""

    revision: str


@dataclass(frozen=True)
class DeployFailed:
    """Named so the journal says which step, not just that something broke."""

    step: str
    message: str


type DeployResult = Deployed | AlreadyCurrent | DeployFailed


def repo_owner() -> str:
    return pwd.getpwuid(repo_root().stat().st_uid).pw_name


def _run(argv: list[str], *, as_owner: bool, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a command, dropping to the checkout's owner when asked.

    This process runs as root so it can restart the unit, but anything touching
    the repository or the database has to run as the owner. Doing git or uv
    work as root would leave root-owned files in `.git`, `.venv`, and the WAL
    beside the database — and the service, which runs unprivileged, could then
    no longer write its own data directory.
    """
    if as_owner and os.geteuid() == 0:
        argv = ["runuser", "-u", repo_owner(), "--", *argv]
    return subprocess.run(
        argv,
        cwd=repo_root(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _fail(step: str, completed: subprocess.CompletedProcess[str]) -> DeployFailed:
    detail = (completed.stderr or completed.stdout).strip()
    return DeployFailed(step, detail or f"exit status {completed.returncode}")


def _uv() -> str | None:
    """Find uv, which lives in the owner's home rather than on root's PATH."""
    home = Path(pwd.getpwnam(repo_owner()).pw_dir)
    candidate = home / ".local" / "bin" / "uv"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("uv")


def current_revision() -> str:
    result = _run(["git", "rev-parse", "--short", "HEAD"], as_owner=True, timeout=30)
    return result.stdout.strip() or "unknown"


def deploy() -> DeployResult:
    """One deployment attempt. Safe to call when there is nothing to do."""
    branch = deploy_branch()

    on_branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], as_owner=True, timeout=30)
    if on_branch.returncode != 0:
        return _fail("checking the current branch", on_branch)
    if on_branch.stdout.strip() != branch:
        # Someone is working on the server by hand. Deploying would yank the
        # checkout out from under them.
        return DeployFailed(
            "checking the current branch",
            f"checkout is on {on_branch.stdout.strip()!r}, not {branch!r}; leaving it alone",
        )

    fetched = _run(
        ["git", "fetch", "--quiet", "origin", branch],
        as_owner=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if fetched.returncode != 0:
        return _fail("fetching from origin", fetched)

    local = _run(["git", "rev-parse", "HEAD"], as_owner=True, timeout=30)
    remote = _run(["git", "rev-parse", f"origin/{branch}"], as_owner=True, timeout=30)
    if local.returncode != 0 or remote.returncode != 0:
        return _fail("comparing revisions", local if local.returncode else remote)
    if local.stdout.strip() == remote.stdout.strip():
        return AlreadyCurrent(current_revision())

    logger.info("Deploying %s -> %s", local.stdout.strip()[:7], remote.stdout.strip()[:7])

    # --ff-only, never a merge or a reset: if the checkout has diverged or has
    # uncommitted edits, that is a person's work and this must not discard it.
    merged = _run(
        ["git", "merge", "--ff-only", f"origin/{branch}"],
        as_owner=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if merged.returncode != 0:
        return _fail("fast-forwarding the checkout", merged)

    uv = _uv()
    if uv is None:
        return DeployFailed("syncing dependencies", "uv is not installed for the service user")
    synced = _run(
        [uv, "sync", "--frozen", "--no-dev"],
        as_owner=True,
        timeout=SYNC_TIMEOUT_SECONDS,
    )
    if synced.returncode != 0:
        return _fail("syncing dependencies", synced)

    # Migrations before the restart, and a failure here stops the deploy. The
    # service is still running the previous code from memory, which matches the
    # database it has; restarting into new code against an unmigrated schema
    # would turn a failed deploy into an outage.
    migrated = _run(
        [str(repo_root() / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        as_owner=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if migrated.returncode != 0:
        return _fail("applying migrations", migrated)

    unit_error = refresh_units()
    if unit_error is not None:
        return unit_error

    restarted = _run(
        ["systemctl", "restart", SERVICE_NAME],
        as_owner=False,
        timeout=RESTART_TIMEOUT_SECONDS,
    )
    if restarted.returncode != 0:
        return _fail("restarting the service", restarted)

    return Deployed(current_revision())


def refresh_units() -> DeployFailed | None:
    """Reinstall the systemd units if their templates changed in this pull.

    Without this, a change to the unit — a new `ReadWritePaths` entry, say —
    would sit in the repository and never reach systemd, and the failure would
    show up much later as a permission error inside an agent run.

    Safe to run as root because `install.service_user()` reads the checkout's
    owner rather than the current user.
    """
    # Imported here rather than at module scope: install.py pulls in httpx and
    # alembic, and this function is skipped on the overwhelmingly common
    # already-up-to-date path.
    from workbench.install import install_units, systemd_is_running

    if not systemd_is_running():
        return None

    try:
        install_units()
    except Exception as error:
        # install.py signals failure by raising, so this is the boundary where
        # that becomes a result again. Broad on purpose: a deploy must report
        # every failure into the journal rather than exit on a traceback.
        return DeployFailed("installing systemd units", str(error))
    return None


def main() -> int:
    configure_console_logging()

    if not (repo_root() / ".git").exists():
        logger.error("%s is not a git checkout; nothing to deploy.", repo_root())
        return 1

    try:
        result = deploy()
    except subprocess.TimeoutExpired as error:
        logger.error("Deploy timed out running %s", error.cmd)
        return 1

    match result:
        case AlreadyCurrent(revision):
            logger.info("Already up to date at %s.", revision)
            return 0
        case Deployed(revision):
            logger.info(
                "Deployed %s. Service restarted and listening on %s:%s.",
                revision,
                host(),
                port(),
            )
            return 0
        case DeployFailed(step, message):
            logger.error("Deploy failed while %s:\n%s", step, message)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
