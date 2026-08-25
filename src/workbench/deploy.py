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
import pwd
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Imported as a module, not by name. These are reached through the module
# attribute at call time so that a test patching `workbench.install.x` is
# actually patching what this calls — with names bound at import, it would not
# be, and the first thing to notice would be a unit test shelling out to sudo.
from workbench import install
from workbench.config import (
    database_path,
    deploy_branch,
    ensure_data_dir,
    host,
    port,
    repo_root,
    restore_from,
    service_name,
)
from workbench.logs import configure_console_logging

logger = logging.getLogger(__name__)

#: Long enough for a cold `uv sync` that has to fetch the SDK's ~310MB bundled
#: binary over a home connection.
SYNC_TIMEOUT_SECONDS = 900
GIT_TIMEOUT_SECONDS = 300
RESTART_TIMEOUT_SECONDS = 120

#: Acceptance drives the app over HTTP and reports to GitHub; generous so a
#: slow check never looks like a failed deploy.
ACCEPTANCE_TIMEOUT_SECONDS = 600

#: staging_acceptance.py's exit code for "the checks ran, the verdict did not
#: reach GitHub". Kept in step with EXIT_NOT_REPORTED there.
ACCEPTANCE_EXIT_NOT_REPORTED = 3


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


def repo_owner() -> pwd.struct_passwd:
    """The account that owns the checkout, and therefore owns its files."""
    return pwd.getpwuid(repo_root().stat().st_uid)


def _owner_environment() -> dict[str, str]:
    """The environment a command sees when run as the checkout's owner.

    The installer owns the implementation, because it needs exactly the same
    thing and two copies of "become the service account" is two places for it
    to drift — one of which would then be creating root-owned files in a
    directory the service has to write.
    """
    return install.owner_environment()


def _run(argv: list[str], *, as_owner: bool, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a command, dropping to the checkout's owner when asked.

    This process runs as root so it can restart the unit, but anything touching
    the repository or the database has to run as the owner. Doing git or uv
    work as root would leave root-owned files in `.git`, `.venv`, and the WAL
    beside the database — and the service, which runs unprivileged, could then
    no longer write its own data directory.

    The drop itself lives in `install.service_run`, because the installer needs
    exactly the same thing and two copies of "become the service account" is
    two places for it to drift — one of which would then be creating root-owned
    files in a directory the service has to write.
    """
    if as_owner:
        return install.service_run(argv, timeout=timeout)

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
    home = Path(repo_owner().pw_dir)
    candidate = home / ".local" / "bin" / "uv"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("uv")


def _python() -> Path:
    return repo_root() / ".venv" / "bin" / "python"


def _alembic() -> Path:
    return repo_root() / ".venv" / "bin" / "alembic"


def restore_snapshot() -> DeployFailed | None:
    """Copy another instance's database over this one, before migrating.

    Only staging does this, and only because it is the point: migrating a copy
    of production is the one test that catches a revision which passes against
    an empty database and fails against real rows.

    Uses sqlite3's own backup API rather than copying the file. The source is a
    live database in WAL mode, where the committed state is split between the
    file and its write-ahead log — a plain copy can capture a torn version of
    it. This also needs no `sqlite3` binary, which neither machine has.
    """
    source = restore_from()
    if source is None:
        return None

    if not source.is_file():
        return DeployFailed(
            "restoring the database snapshot",
            f"{source} does not exist, so there is nothing to restore from",
        )

    target = database_path()
    if source == target:
        # Refusing rather than clobbering: this would be production restoring
        # over itself, which is only ever a misconfiguration.
        return DeployFailed(
            "restoring the database snapshot",
            f"WORKBENCH_RESTORE_FROM points at this instance's own database ({target})",
        )

    ensure_data_dir()
    logger.info("Restoring a snapshot of %s over %s", source, target)
    try:
        with sqlite3.connect(source) as origin, sqlite3.connect(target) as replica:
            origin.backup(replica)
    except sqlite3.Error as error:
        return DeployFailed("restoring the database snapshot", str(error))

    return None


def current_revision() -> str:
    result = _run(["git", "rev-parse", "--short", "HEAD"], as_owner=True, timeout=30)
    return result.stdout.strip() or "unknown"


@dataclass(frozen=True)
class Advanced:
    """The checkout moved. Nothing has been rebuilt or restarted yet."""

    revision: str


type AdvanceResult = Advanced | AlreadyCurrent | DeployFailed


def advance_checkout() -> AdvanceResult:
    """Decide whether to deploy, and move the checkout if so.

    Separated from rebuilding because this half is where every refusal lives —
    wrong branch, dirty tree, diverged history — and because it is the only
    half that can be exercised without root, a virtualenv, and systemd.
    """
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

    dirty = _run(
        # --untracked-files=no on purpose: a stray log or scratch file is not
        # work in progress, and git refuses on its own if an incoming commit
        # would actually overwrite an untracked path.
        ["git", "status", "--porcelain", "--untracked-files=no"],
        as_owner=True,
        timeout=30,
    )
    if dirty.returncode != 0:
        return _fail("checking for local changes", dirty)
    if dirty.stdout.strip():
        # Checked explicitly rather than left to `git merge --ff-only`, which
        # only refuses when the incoming commit happens to touch the same file
        # someone edited. That makes "is my work safe" depend on what the
        # commit contains, which is not something anyone can reason about from
        # the server. Refusing on any modification is the promise that is
        # actually keepable.
        changed = ", ".join(line[3:] for line in dirty.stdout.strip().splitlines()[:5])
        return DeployFailed(
            "checking for local changes",
            f"the checkout has uncommitted changes ({changed}); leaving it alone",
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

    # --ff-only, never a merge or a reset. Uncommitted work is already
    # refused above; this is the remaining case, a checkout carrying local
    # commits, which must not be rewritten either.
    merged = _run(
        ["git", "merge", "--ff-only", f"origin/{branch}"],
        as_owner=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if merged.returncode != 0:
        return _fail("fast-forwarding the checkout", merged)

    return Advanced(current_revision())


def rebuild_and_restart() -> DeployFailed | None:
    """Bring the installed service in line with the checkout as it now stands.

    Only reached once the checkout has actually moved, so it is never paying
    for a sync and a restart on the overwhelmingly common no-op tick.
    """
    uv = _uv()
    if uv is None:
        return DeployFailed(
            "syncing dependencies", "uv is not installed for the account that owns the checkout"
        )
    synced = _run(
        [uv, "sync", "--frozen", "--no-dev"],
        as_owner=True,
        timeout=SYNC_TIMEOUT_SECONDS,
    )
    if synced.returncode != 0:
        return _fail("syncing dependencies", synced)

    # Preflight before anything irreversible. Importing the app in a throwaway
    # process catches syntax errors, bad imports, and a missing dependency
    # while the running service is still untouched — most crash-on-boot
    # failures never get as far as the restart.
    preflight = _run(
        [str(_python()), "-c", "import workbench.app"],
        as_owner=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if preflight.returncode != 0:
        return _fail("importing the new code", preflight)

    restore_error = restore_snapshot()
    if restore_error is not None:
        return restore_error

    # Migrations before the restart, and a failure here stops the deploy. The
    # service is still running the previous code from memory, which matches the
    # database it has; restarting into new code against an unmigrated schema
    # would turn a failed deploy into an outage.
    migrated = _run(
        [str(_alembic()), "upgrade", "head"],
        as_owner=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if migrated.returncode != 0:
        return _fail("applying migrations", migrated)

    # Models and migrations agreeing is checked here rather than trusted from
    # CI: a merge that skipped a revision would otherwise only surface as
    # confusing runtime errors after the restart.
    drift = _run([str(_alembic()), "check"], as_owner=True, timeout=GIT_TIMEOUT_SECONDS)
    if drift.returncode != 0:
        return _fail("checking for schema drift", drift)

    unit_error = refresh_units()
    if unit_error is not None:
        return unit_error

    restarted = _run(
        ["systemctl", "restart", service_name()],
        as_owner=False,
        timeout=RESTART_TIMEOUT_SECONDS,
    )
    if restarted.returncode != 0:
        return _fail("restarting the service", restarted)

    # The step this used to be missing. Without it a deploy that installs
    # code which dies on startup reports success into the journal while
    # Restart=always thrashes, and the first sign of trouble is the app being
    # unreachable from a phone.
    unhealthy = install.health_check()
    if unhealthy is not None:
        return DeployFailed("waiting for the service to come back", unhealthy)

    return None


def deploy() -> DeployResult:
    """One deployment attempt. Safe to call when there is nothing to do."""
    advanced = advance_checkout()
    if not isinstance(advanced, Advanced):
        if isinstance(advanced, AlreadyCurrent):
            # Converge the units even with nothing to pull.
            #
            # A change to the *deployer* only takes effect on the deploy after
            # the one that delivered it: this process imported its own code
            # before pulling, so the run that brings in a new install step is
            # the last run that does not perform it. Without this line the new
            # step then waits for an unrelated commit to come along, and in the
            # meantime the machine is running code whose install half never
            # happened.
            #
            # Installing is idempotent and compares rendered content before
            # writing, so on the overwhelmingly common no-op tick this reads
            # four templates and does nothing. That also makes it
            # self-healing: a unit or rule deleted by hand comes back.
            failure = refresh_units()
            if failure is not None:
                return failure
        return advanced

    failure = rebuild_and_restart()
    if failure is not None:
        return failure

    run_acceptance()
    return Deployed(advanced.revision)


def run_acceptance() -> None:
    """Where the data is disposable: exercise what just deployed, and report it.

    **Gated on `restore_from()`, not on being a non-production instance.**
    Acceptance drives the app over HTTP and creates records as it goes, so it
    can only run where losing data is fine. An instance that replaces its whole
    database from a snapshot on every deploy is exactly that, and nothing else
    is — so the same setting that makes staging disposable is the one that
    makes running this against it safe.

    Testing `instance()` instead would mean any second install gets its data
    quietly mutated by a deploy. That is not hypothetical: it is how this was
    found, when the CI instance's seeded rows were rewritten mid-test.

    Deliberately does not change the deploy's own result. Failing acceptance is
    not a failed *deploy* — the code is installed and running, which is exactly
    the state someone needs in order to go and look at what broke. What it does
    instead is post a red commit status, which is what stops promotion.

    Runs as the checkout's owner rather than as root: it opens the database,
    and SQLite would leave root-owned WAL files beside it that the
    unprivileged service could then not write.
    """
    if restore_from() is None:
        return

    script = repo_root() / "scripts" / "staging_acceptance.py"
    if not script.is_file():
        logger.warning("No acceptance script at %s; skipping.", script)
        return

    logger.info("Running staging acceptance")
    result = _run(
        [str(_python()), str(script)],
        as_owner=True,
        timeout=ACCEPTANCE_TIMEOUT_SECONDS,
    )
    logger.info("%s", (result.stdout or result.stderr).strip())

    if result.returncode == ACCEPTANCE_EXIT_NOT_REPORTED:
        logger.warning(
            "Acceptance ran but its verdict never reached GitHub — check "
            "WORKBENCH_GITHUB_TOKEN in /etc/workbench/env. Promotion waits on that "
            "status, so it will stall with nothing obviously wrong."
        )
    elif result.returncode != 0:
        logger.warning(
            "Staging acceptance failed. The deploy itself succeeded; the red commit "
            "status is what blocks promotion."
        )


def refresh_units() -> DeployFailed | None:
    """Reinstall the systemd units and the polkit rule, if either changed.

    Without this, a change to the unit — a new `ReadWritePaths` entry, say —
    would sit in the repository and never reach systemd, and the failure would
    show up much later as a permission error inside an agent run.

    The polkit rule is here for exactly that reason, and it was missed the
    first time: an install by hand wrote it, but a deploy did not, so a machine
    updated automatically would grow the ability to *ask* systemd to start a
    run unit without the authorisation to have it granted. Every run would fail
    with access denied, and the fix would have been a manual `install.sh` — the
    kind of remembered step this whole file exists to abolish.

    Safe to run as root, and now load-bearing rather than incidental:
    `install.service_user()` reads the *checkout's owner*, which after the move
    to /srv is the dedicated service account. Reading the effective uid here
    would re-render every unit with `User=root` on the first automatic deploy
    and hand an agent a root shell.
    """
    if not install.systemd_is_running():
        return None

    try:
        install.install_units()
        install.install_polkit_rule()
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
