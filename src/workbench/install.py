"""Everything both installs are made of: an account, a deployment, units.

There are two entry points, not one — `install_core` for the machine that runs
Workbench, `install_node` for a machine that lends it a GPU — and this module
is what they share. It has no `main()` of its own on purpose: a single
installer with a role flag threaded through it was the alternative, and the
half a reader has to hold in their head while reading the other half is
exactly what that costs.

Run via ./install.sh, which installs uv, picks the entry point from `--role`,
and hands off. Written in Python rather than shell so that it can import
workbench.config — the port and database path come from the same place the
running app reads them, rather than being repeated in a second language where
they can drift.

Idempotent: every step checks before acting, so re-running is a no-op.

**Nothing here imports anything but the standard library, and that is load
bearing.** This module runs before a virtualenv exists, because one of the
first things it may decide is that this checkout is not where the deployment
belongs — and building a ~310MB environment in a directory it is about to
abandon is the one cost worth going out of the way to avoid. `install.sh`
therefore starts it with `uv run --no-project`, and the environment is built
later, at the deployment, by the account that will own it.

**Two identities, and the distinction is the whole design.** The install runs
as root, because it creates an account and writes to /etc. Everything touching
the checkout, the virtualenv, the database, or the service account's home runs
as *that account*, through `service_run`. Doing any of it as root would leave
root-owned files that the unprivileged service could no longer write — a
failure that arrives later, as a service that starts and then cannot save
anything.
"""

import logging
import os
import pwd
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from workbench.config import (
    RUN_TIMEOUT_SECONDS,
    agent_git_identity,
    agent_home,
    data_dir,
    deploy_branch,
    deploy_unit_name,
    deployment_root,
    head_marker,
    host,
    instance,
    is_node,
    port,
    repo_root,
    role_marker,
    run_unit_prefix,
    service_account,
    service_name,
)
from workbench.logs import BOLD, YELLOW, paint

logger = logging.getLogger(__name__)

SYSTEMD_DIR = Path("/etc/systemd/system")
TEMPLATE_DIR = Path("deploy")
HEALTH_TIMEOUT_SECONDS = 20.0

#: How often the deployer checks for new commits. Not configurable through the
#: environment: it is baked into the timer at install time, and changing it
#: means re-rendering the unit anyway.
#:
#: The schedule itself is written directly into the timer template rather than
#: rendered from here — see the note there. This is only the wording for the
#: messages a person reads, and the two are kept in step by a test.
DEPLOY_INTERVAL = "5min"


def units() -> tuple[tuple[str, str], ...]:
    """Every unit this instance installs, as (unit filename, template name).

    Computed rather than constant for two reasons. The names carry the
    instance: a staging install writes `workbench-staging.service` alongside
    production's `workbench.service`, and the two must never resolve to the
    same file. And they carry the role: a node runs no app and executes no
    runs, so installing those units there would leave a service failing on a
    database that does not exist.

    Read by the deployer as well as the installer, which is why it consults
    `config.role()` rather than taking an argument — the deploy that re-renders
    units on a node has nobody to pass one.
    """
    deployer = (
        (f"{deploy_unit_name()}.service", "workbench-deploy.service.template"),
        (f"{deploy_unit_name()}.timer", "workbench-deploy.timer.template"),
    )
    if is_node():
        # The timer and nothing else. Self-updating is not optional on a node:
        # a machine that cannot pull its own changes is a permanent manual
        # step, which is the thing this project exists not to have.
        return deployer
    return (
        (f"{service_name()}.service", "workbench.service.template"),
        *deployer,
        # A template unit, never enabled: started on demand as
        # `workbench-run@<run id>.service`. One per run, so each gets its own
        # cgroup and survives the app restarting under it.
        (f"{run_unit_prefix()}@.service", "workbench-run@.service.template"),
    )


def step(message: str) -> None:
    logger.info("\n%s", paint(BOLD, f"==> {message}"))


def info(message: str) -> None:
    logger.info("    %s", message)


def warn(message: str) -> None:
    logger.warning("    %s %s", paint(YELLOW, "warning:"), message)


class InstallError(Exception):
    """Something went wrong that the user needs to act on."""


def run(
    argv: list[str], *, privileged: bool = False, stream: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a command, escalating with sudo only when asked and only if needed.

    `stream` lets a slow command narrate. Output is captured by default so that
    a failure can be folded into an `InstallError`, but a five-minute download
    behind a captured pipe looks exactly like a hang.
    """
    if privileged and os.geteuid() != 0:
        if shutil.which("sudo") is None:
            raise InstallError(f"need root to run {' '.join(argv)}, and sudo is not installed")
        argv = ["sudo", *argv]

    result = subprocess.run(argv, capture_output=not stream, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InstallError(f"`{' '.join(argv)}` failed:\n       {detail}")
    return result


def owner_environment() -> dict[str, str]:
    """The environment a command should see when run as the checkout's owner.

    Dropping privileges with setuid does not change the environment the way a
    login would, so HOME would still point at root's. That matters: uv resolves
    its cache from HOME, git looks there for user configuration, and under a
    subscription the agent's credential lives there — so a wrong HOME is not an
    inconvenience, it is the install writing another account's files.
    """
    owner = _service_passwd()
    return {**os.environ, "HOME": owner.pw_dir, "USER": owner.pw_name, "LOGNAME": owner.pw_name}


def service_run(
    argv: list[str],
    *,
    timeout: int | None = None,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command as the account that owns the checkout.

    The install runs as root so it can create an account and write to /etc.
    Anything touching the checkout, the virtualenv, the database, or that
    account's home has to run as the account instead: root-owned files there
    are ones the unprivileged service can no longer write, and that failure
    arrives later, as a service which starts and then cannot save anything.

    Uses subprocess's own `user=` rather than shelling out to `runuser`. Both
    end up calling setuid, but runuser opens a PAM session to get there and PAM
    logs every one — three lines per command into the journal that is the only
    place a bad deploy explains itself. This also spawns one process, not two.

    Returns the result rather than raising, because the deployer shares this
    and reports failures into the journal instead of dying on them.
    """
    privileged_kwargs = {}
    if os.geteuid() == 0:
        owner = _service_passwd()
        privileged_kwargs = {
            "user": owner.pw_uid,
            "group": owner.pw_gid,
            # Supplementary groups are not inherited across setuid, and leaving
            # root's would hand the child more access than the owner has.
            "extra_groups": [],
            "env": owner_environment(),
        }

    return subprocess.run(
        argv,
        cwd=repo_root(),
        capture_output=not stream,
        text=True,
        timeout=timeout,
        check=False,
        **privileged_kwargs,
    )


def service_run_or_fail(argv: list[str], what: str, *, stream: bool = False) -> None:
    """`service_run`, but for the installer, which does raise."""
    result = service_run(argv, stream=stream)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise InstallError(f"{what} failed:\n       {detail}")


def check_invocation() -> None:
    """Refuse the one way of running this that cannot work.

    Running the installer *as* the service account fails halfway through: that
    account deliberately has no sudo, so it would create the deployment and
    then be unable to write a single unit — leaving a machine in a state that
    is harder to reason about than either end.

    Note what is no longer refused. `sudo ./install.sh` used to be, on the
    grounds that it left a root-owned virtualenv and a service running as root.
    Both are now handled properly rather than avoided: the venv is built by
    `service_run` as the account that will own it, and `User=` comes from the
    checkout's owner rather than from whoever is running this. Root is the
    normal path here, because creating an account requires it.
    """
    try:
        running = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:  # a uid with no passwd entry; nothing to compare against
        return

    if running == service_account():
        raise InstallError(
            f"Do not run this as '{service_account()}'.\n"
            "       That account has no sudo on purpose, so the install would stop\n"
            "       partway through with some units written and others not.\n"
            "       Run it as yourself:  ./install.sh"
        )


def become_root(entry: str) -> None:
    """Re-exec under sudo, because everything from here needs it.

    Creating an account, writing under /srv, and chowning a tree to another
    user are all root's work, and asking once up front is better than a dozen
    separate `sudo` calls whose password prompts are invisible behind captured
    output — which reads as a hang rather than a prompt.

    `env` sits in front of the command deliberately. sudo scrubs PYTHONPATH
    from the environment it passes on, and this installer is running out of
    `src/` with nothing installed, so losing it would leave the re-exec unable
    to import itself.
    """
    if os.geteuid() == 0:
        return

    if shutil.which("sudo") is None:
        raise InstallError("this install needs root, and sudo is not installed")

    forwarded = [
        f"{name}={value}" for name, value in os.environ.items() if name.startswith("WORKBENCH_")
    ]
    forwarded.append(f"PYTHONPATH={os.environ.get('PYTHONPATH', '')}")
    # Resolved before escalating: sudo resets PATH to secure_path, and uv lives
    # in the invoking user's home rather than anywhere on root's.
    forwarded.append(f"WORKBENCH_UV={shutil.which('uv') or ''}")

    info("escalating with sudo for the rest of the install")
    # `entry` rather than a fixed module: the role is which installer is
    # running, so re-exec the one that is running rather than trusting a flag
    # to survive sudo's environment scrubbing.
    argv = ["sudo", "env", *forwarded, sys.executable, "-m", entry, *sys.argv[1:]]
    os.execvp("sudo", argv)


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
    """Bring the database to head, as the account that owns it.

    Through the virtualenv's alembic rather than its Python API, which is the
    same shape the deployer already uses. Two reasons, and only one of them is
    tidiness. This module cannot import alembic at all any more — it runs
    before a virtualenv exists. And running the upgrade in-process would run it
    as root, leaving a root-owned database and, worse, root-owned `-wal` and
    `-shm` files beside it: the service would start, read happily, and fail on
    the first write.
    """
    service_run_or_fail([str(_venv_bin("alembic")), "upgrade", "head"], "applying migrations")


def ensure_data_directory(account: pwd.struct_passwd) -> None:
    """Create `data/`, owned by the account that has to write it.

    This is the same trap as everything else in this file, and it caught me:
    `ensure_data_dir()` is an ordinary in-process mkdir, and this process is
    root — so a fresh install left `data/` owned by root and the service could
    not create its own database inside it. Migrations then failed with
    "unable to open database file", which names neither the directory nor the
    ownership that caused it.

    A directory that already exists with the wrong owner is repaired rather
    than left, because that is precisely the state the bug above produced.

    Ownership is set by numeric uid and gid throughout this file rather than by
    name. `-g <name>` quietly assumes a user-private group, which `useradd
    --system` does give us — but the account is only ours when we created it,
    and a checkout owned by a person on a machine whose primary group is
    something else would fail on a group that does not exist.
    Deliberately not recursive: `repos/` and `worktrees/` are made by the
    service itself and are already right, and recursing into a few gigabytes
    of clones to fix a directory would be a poor trade.
    """
    target = data_dir()
    if not target.is_dir():
        run(
            ["install", "-d", "-o", str(account.pw_uid), "-g", str(account.pw_gid), str(target)],
            privileged=True,
        )
        info(f"created {target}")
        return

    if target.stat().st_uid == account.pw_uid:
        return

    owned = f"{account.pw_uid}:{account.pw_gid}"
    run(["chown", owned, str(target)], privileged=True)
    for entry in sorted(target.iterdir()):
        if entry.is_file():
            run(["chown", owned, str(entry)], privileged=True)
    info(f"{target} now belongs to {account.pw_name}")


def record_role(name: str, account: pwd.struct_passwd) -> None:
    """Write down what this machine was installed as.

    Read back by `config.role()`, which every later question keys off: which
    units belong here, what a deploy should do, which doctor checks apply. A
    file rather than a unit's environment, because a person running the doctor
    by hand on a node must get the node's answer, and an environment variable
    that only exists inside systemd would give them the head's.

    Owned by the service account like everything else under `data/` — this
    runs as root, and a root-owned marker in a directory the service rewrites
    is the same trap as a root-owned database.
    """
    marker = role_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{name}\n", encoding="utf-8")
    os.chown(marker, account.pw_uid, account.pw_gid)
    info(f"this machine is a {name}")


def record_head(url: str, account: pwd.struct_passwd) -> None:
    """Write down which head this node reports to. See `record_role`."""
    marker = head_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{url}\n", encoding="utf-8")
    os.chown(marker, account.pw_uid, account.pw_gid)
    info(f"reporting to the head at {url}")


def _service_passwd() -> pwd.struct_passwd:
    """The passwd entry of whoever owns the checkout.

    Derived from the repo's owner rather than from `os.geteuid()`, because this
    is also called by the automatic deployer, which runs as root in order to
    restart the unit. Reading the effective uid there would silently re-render
    the unit with `User=root` and hand an agent a root shell on the next
    deploy. The checkout's owner is the same answer in the interactive case and
    the right one in both.
    """
    return pwd.getpwuid(repo_root().stat().st_uid)


def service_user() -> str:
    """Who the service runs as."""
    return _service_passwd().pw_name


def service_home() -> Path:
    """The service user's home directory.

    Rendered into the unit rather than left to systemd's `%h`, which resolves
    to the home of the *manager* — `/root` for a system service — no matter
    what `User=` says. Under an agent workload that would point the credential
    path at root's home, where the service cannot read it.
    """
    return Path(_service_passwd().pw_dir)


def agent_state_dir() -> Path:
    """Where a backend keeps credentials and session transcripts.

    The subscription credential lives here rather than in `/etc/workbench/env`,
    which is what "runs bill a subscription" concretely means on disk: the home
    directory of the user the unit runs as is the thing that decides who pays.

    It has to be writable, not merely readable, and that is easy to get wrong
    because it fails *late* — an OAuth token is refreshed periodically, so a
    read-only path works for days and then stops. Both this directory and the
    `ReadWritePaths` entry in the unit exist for that one reason.
    """
    return service_home() / ".claude"


def _venv_bin(name: str) -> Path:
    """A program inside the deployment's virtualenv."""
    return repo_root() / ".venv" / "bin" / name


def uv_binary() -> str:
    """Where uv is, surviving the escalation that loses it.

    sudo resets PATH to `secure_path`, and uv installs itself into the invoking
    user's home — so by the time this runs as root, `which uv` finds nothing.
    `become_root` therefore resolves it beforehand and forwards it.
    """
    forwarded = os.environ.get("WORKBENCH_UV", "").strip()
    if forwarded and Path(forwarded).is_file():
        return forwarded

    found = shutil.which("uv")
    if found is None:
        raise InstallError("uv is not on PATH; run ./install.sh rather than this module")
    return found


def ensure_service_account() -> pwd.struct_passwd:
    """Create the account the units run as, if it is not there already.

    A system account: no password, no login, and nothing else on the machine
    belongs to it. That is the whole security claim — an agent executes
    model-authored shell commands, and the account it runs as is the only real
    bound on what those commands can reach.

    It gets a real shell despite not being loginable, because `sudo -iu` needs
    one, and signing the agent in is a step someone has to perform *as this
    account*. A `nologin` shell would make the one manual step in the install
    impossible to carry out.
    """
    account = service_account()
    try:
        return pwd.getpwnam(account)
    except KeyError:
        pass

    run(
        [
            "useradd",
            "--system",
            "--create-home",
            "--home-dir",
            str(agent_home()),
            "--shell",
            "/bin/bash",
            "--comment",
            f"Workbench service ({instance() or 'production'})",
            account,
        ],
        privileged=True,
    )
    # 0750: the account's home holds the agent's credential, and nothing else
    # on the machine has any business reading it.
    run(["chmod", "0750", str(agent_home())], privileged=True)
    info(f"created system account '{account}' with home {agent_home()}")
    return pwd.getpwnam(account)


#: Never copied into a deployment. `.venv` bakes absolute paths into its
#: scripts, so a copied one would point back at the checkout it came from;
#: the caches are build products the new location makes for itself.
RELOCATION_SKIPS = frozenset(
    {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)


def _is_database(name: str) -> bool:
    """A SQLite database or one of its sidecars."""
    return name.endswith(".db") or ".db-" in name


def _relocation_ignore(source: Path):
    """What `copytree` skips, beyond the build products above.

    `data/worktrees` is disposable by design and its git metadata names the old
    path, so copying it would carry a broken reference into the new tree. The
    databases are skipped here and copied separately, because a live database
    in WAL mode cannot safely be copied as a file.
    """

    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if name in RELOCATION_SKIPS}
        if Path(directory).resolve() == (source / "data").resolve():
            skipped.add("worktrees")
            skipped.update(name for name in names if _is_database(name))
        return skipped

    return ignore


def copy_database(source: Path, target: Path) -> None:
    """Copy a SQLite database through its own backup API, never as a file.

    The database being copied is usually live and in WAL mode, which means
    committed data is split between the file and its write-ahead log. `cp`
    produces something either stale or torn; this is the only way to get a
    consistent snapshot of a database another process has open.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    origin = sqlite3.connect(source)
    copy = sqlite3.connect(target)
    try:
        origin.backup(copy)
    finally:
        copy.close()
        origin.close()


def _chown_tree(root: Path, account: pwd.struct_passwd) -> None:
    os.chown(root, account.pw_uid, account.pw_gid)
    for path in root.rglob("*"):
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)


def needs_relocation() -> bool:
    return repo_root() != deployment_root()


def relocate(account: pwd.struct_passwd) -> Path:
    """Copy this checkout to where the deployment lives, owned by the account.

    **Copies rather than moves, and that is deliberate.** The checkout someone
    cloned is left exactly as it was, so this needs no confirmation, breaks
    nothing if it goes wrong, and leaves the pre-existing database sitting
    there as a free point-in-time backup. Rolling back is re-running the old
    checkout's installer with WORKBENCH_DEPLOYMENT_ROOT pointing at itself.

    A deployment that is already there and already owned by the account is left
    alone and handed off to, which is what makes re-running the *abandoned*
    checkout's `install.sh` harmless rather than destructive.
    """
    source = repo_root()
    target = deployment_root()

    # Recognised by `pyproject.toml`, which is exactly what `repo_root()` keys
    # on — so "already a deployment" means the same thing here as it does to
    # every other path in the codebase. Keying on `.git` instead looked
    # equivalent and was not: a tree delivered by anything other than a clone
    # has no `.git`, the guard silently never fires, and the install copies a
    # stale checkout over the live deployment. Found by the container harness,
    # which delivers exactly such a tree.
    if (target / "pyproject.toml").is_file() and target.stat().st_uid == account.pw_uid:
        info(f"deployment already exists at {target}")
        return target

    # Stopped first, so nothing is writing the database while it is copied.
    # Best effort: on a first install there is nothing to stop.
    if systemd_is_running():
        for unit in (f"{service_name()}.service", f"{deploy_unit_name()}.timer"):
            subprocess.run(["systemctl", "stop", unit], capture_output=True, text=True)

    info(f"copying {source} to {target}")
    shutil.copytree(source, target, ignore=_relocation_ignore(source), dirs_exist_ok=True)

    for database in sorted((source / "data").glob("*.db")):
        copy_database(database, target / "data" / database.name)
        info(f"copied {database.name} through SQLite's backup API")

    (target / "data").mkdir(parents=True, exist_ok=True)
    _chown_tree(target, account)
    info(f"{target} now belongs to {account.pw_name}")

    _forget_stale_worktrees(target, source)
    return target


def _forget_stale_worktrees(target: Path, source: Path) -> None:
    """Drop worktree paths that point back at the checkout we came from.

    Worktrees are disposable by design and were not copied, so the rows naming
    them are now pointing at directories outside the deployment which the
    service account cannot write. Clearing the path means the next run makes a
    fresh worktree; the branch is untouched, so no work is lost.

    Done here rather than in a migration because it is a property of *this
    machine's* relocation, not of the schema — the same database restored onto
    another instance would need different paths, or none.
    """
    database = target / "data" / "workbench.db"
    if not database.is_file():
        return

    connection = sqlite3.connect(database)
    try:
        cleared = connection.execute(
            "UPDATE tasks SET worktree_path = NULL WHERE worktree_path LIKE ?",
            (f"{source}%",),
        ).rowcount
        connection.commit()
    finally:
        connection.close()

    if cleared:
        info(f"cleared {cleared} worktree path(s) that pointed at {source}")


def hand_off_to(target: Path, entry: str) -> None:
    """Re-exec the installer from the deployment, and never come back.

    `repo_root()` is derived from this module's own location, so simply
    changing directory would leave every later step operating on the checkout
    we just left. The code at the new path is byte-identical, but it is the
    code that belongs to the deployment, and running it is what makes every
    path below resolve there.
    """
    environment = {
        **os.environ,
        "PYTHONPATH": str(target / "src"),
        # A loop guard. If the copy silently landed somewhere else, this stops
        # the second pass from relocating again, and again.
        "WORKBENCH_RELOCATED": "1",
    }
    step(f"Continuing the install at {target}")
    os.chdir(target)
    # Same module that got us here, for the same reason `become_root` takes
    # one: a node that relocated and came back as a head would install the
    # wrong units on the machine it had just moved to.
    argv = [sys.executable, "-m", entry, *sys.argv[1:]]
    os.execve(sys.executable, argv, environment)


def ensure_uv_for_owner(account: pwd.struct_passwd) -> Path:
    """Put uv where the service account can reach it, and return that path.

    Two things need this and both are easy to miss. `deploy._uv()` looks in the
    owner's `~/.local/bin` before PATH, so without it every automatic deploy
    fails at `uv sync` on a machine where the install worked. And the install
    itself builds the virtualenv *as this account*, which cannot read the uv
    that got us here — it lives in the invoking user's home, and after a
    `sudo` that is `/root`, mode 0700.

    Copied rather than re-downloaded: it needs no network, and it guarantees
    the uv the deployer uses is the one that built this virtualenv.
    """
    home = Path(account.pw_dir)
    target = home / ".local" / "bin" / "uv"
    if target.is_file():
        return target

    # The parent directories are created explicitly and owned by the account.
    # `install -D` would make them too, but as *root* — and uv then cannot
    # create its own `~/.local/share/uv` cache beside the binary, which fails
    # as a permission error naming a directory nobody asked for.
    for directory in (target.parent.parent, target.parent):
        run(
            [
                "install",
                "-d",
                "-o",
                str(account.pw_uid),
                "-g",
                str(account.pw_gid),
                "-m",
                "0755",
                str(directory),
            ],
            privileged=True,
        )
    run(
        [
            "install",
            "-o",
            str(account.pw_uid),
            "-g",
            str(account.pw_gid),
            "-m",
            "0755",
            uv_binary(),
            str(target),
        ],
        privileged=True,
    )
    info(f"uv installed at {target}")
    return target


def build_environment(uv: Path) -> None:
    """Create the virtualenv, as the account that will run out of it.

    Takes the account's own copy of uv rather than resolving one, because the
    one this process used is in a home directory the account cannot read.

    Streamed rather than captured: this downloads a few hundred megabytes on a
    first install, and a silent five minutes is indistinguishable from a hang.
    """
    service_run_or_fail(
        [str(uv), "sync", "--frozen", "--no-dev"],
        "installing dependencies",
        stream=True,
    )


def ensure_agent_state_dir(account: pwd.struct_passwd) -> None:
    """Create the directory the backend keeps its credential in.

    Created here rather than left to first use because the service runs under
    ProtectSystem=strict: a directory that does not exist when the unit starts
    cannot be created by anything inside it.

    Created *as the account*, which is the part that is easy to get wrong. A
    root-owned `~/.claude` fails late and confusingly — the agent reads the
    credential fine for days, then cannot save the refreshed one.

    Taken from the account's own passwd entry rather than from `agent_home()`,
    because the two only agree when this install created the account. Pin the
    deployment somewhere else and the service account is whoever owns it, whose
    home is wherever their home actually is.
    """
    target = Path(account.pw_dir) / ".claude"
    if target.is_dir():
        # Ownership only. `install -d -m` re-applies the mode to a directory
        # that already exists, and this path is a *person's* home directory
        # whenever the deployment root is overridden — the rollback in
        # docs/deployment-setup.md does exactly that. Tightening someone's own
        # ~/.claude as a side effect of a re-install is not this function's
        # business.
        run(["chown", f"{account.pw_uid}:{account.pw_gid}", str(target)], privileged=True)
    else:
        run(
            [
                "install",
                "-d",
                "-o",
                str(account.pw_uid),
                "-g",
                str(account.pw_gid),
                "-m",
                "0700",
                str(target),
            ],
            privileged=True,
        )
    info(f"agent state directory ready at {target}")


def ensure_agent_identity(account: pwd.struct_passwd) -> None:
    """Give the account what it needs to commit and to push.

    Both are unattended: an agent commits its own work, and the deployer
    pushes. Neither can answer a prompt, so an unset identity or a missing key
    is not a question — it is a run that dies several minutes in, having
    already done the work.

    Only the automatable half happens here. A keypair can be generated; a
    keypair cannot be *authorised*, because that means pasting the public half
    into a GitHub repository nobody here has credentials for. So this is the
    same bargain as the agent's login: do what can be done, and let the doctor
    name what is left, with the key to paste.

    Nothing already set is overwritten. Someone who has configured this
    account by hand has made a decision, and re-running the installer must not
    quietly undo it.
    """
    name, email = agent_git_identity()
    for setting, value in (("user.name", name), ("user.email", email)):
        existing = service_run(["git", "config", "--global", "--get", setting])
        if existing.returncode == 0 and existing.stdout.strip():
            continue
        service_run_or_fail(
            ["git", "config", "--global", setting, value],
            f"setting git {setting}",
        )
        info(f"git {setting} = {value}")

    ssh_dir = Path(account.pw_dir) / ".ssh"
    run(
        [
            "install",
            "-d",
            "-o",
            str(account.pw_uid),
            "-g",
            str(account.pw_gid),
            "-m",
            "0700",
            str(ssh_dir),
        ],
        privileged=True,
    )

    key = ssh_dir / "id_ed25519"
    if not key.exists():
        service_run_or_fail(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", email, "-f", str(key)],
            "generating an SSH key",
        )
        info(f"generated {key}")

    _trust_github(ssh_dir)


def _trust_github(ssh_dir: Path) -> None:
    """Record GitHub's host key, so the first push is not the first prompt.

    ssh writes `known_hosts` itself on first connection — but the run unit is
    non-interactive and, until recently, could not write its own home under
    ProtectSystem=strict. Doing it here means the first agent push fails for
    reasons to do with the key, if it fails at all, rather than for reasons to
    do with a host it has never met.

    A warning rather than an error when it does not work: this needs the
    network, and an install on a machine that cannot currently reach GitHub is
    not a broken install.
    """
    known_hosts = ssh_dir / "known_hosts"
    if known_hosts.is_file() and "github.com" in known_hosts.read_text():
        return

    scanned = service_run(["ssh-keyscan", "-t", "rsa,ecdsa,ed25519", "github.com"], timeout=30)
    if scanned.returncode != 0 or not scanned.stdout.strip():
        warn("could not reach github.com to record its host key; the first push may fail")
        return

    with known_hosts.open("a") as handle:
        handle.write(scanned.stdout)
    shutil.chown(known_hosts, user=_service_passwd().pw_name, group=_service_passwd().pw_name)
    known_hosts.chmod(0o644)
    info(f"recorded github.com in {known_hosts}")


def systemd_is_running() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()


def render_unit(template_name: str) -> str:
    template = (repo_root() / TEMPLATE_DIR / template_name).read_text()
    replacements = {
        "__REPO__": str(repo_root()),
        "__USER__": service_user(),
        "__HOME__": str(service_home()),
        "__HOST__": host(),
        "__PORT__": str(port()),
        "__BRANCH__": deploy_branch(),
        "__INSTANCE__": instance(),
        "__SERVICE__": service_name(),
        # A staging install restores production's database before migrating;
        # production leaves this empty and never restores anything.
        "__RESTORE_FROM__": os.environ.get("WORKBENCH_RESTORE_FROM", "").strip(),
        "__RUN_PREFIX__": run_unit_prefix(),
        "__RUN_TIMEOUT__": str(RUN_TIMEOUT_SECONDS),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
    return template


def write_privileged(target: Path, content: str, *, staged_as: str) -> None:
    """Write a file under /etc, whichever identity this is running as.

    As root — which is the normal path now — the write is direct. Otherwise it
    goes through `data/` and a privileged `cp`, so an unprivileged caller never
    needs write access to /etc. The indirection is kept rather than deleted
    because the deployer is not the only caller, and a re-run by hand from a
    checkout that has not been relocated is still a supported thing to do.
    """
    if os.geteuid() == 0:
        target.write_text(content)
        return

    staged = repo_root() / "data" / staged_as
    staged.write_text(content)
    try:
        run(["cp", str(staged), str(target)], privileged=True)
    finally:
        staged.unlink(missing_ok=True)


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

        write_privileged(target, rendered, staged_as=f"{unit_name}.staged")
        info(f"wrote {target}")
        changed.add(unit_name)

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


#: Where polkit reads local authorisation rules.
POLKIT_DIR = Path("/etc/polkit-1/rules.d")


def polkit_rule_name() -> str:
    """The rule file for this instance.

    Instance-scoped for the same reason the units are, and it is easy to miss
    because the failure is silent and delayed: production and staging grant
    *different* unit patterns, so a shared filename would mean whichever
    installed last silently revoked the other's ability to start runs.

    Numbered so it is read after the packaged defaults; the directory is shared
    with the rest of the system.
    """
    return f"50-{run_unit_prefix()}.rules"


def polkit_rule_path() -> Path:
    return POLKIT_DIR / polkit_rule_name()


def install_polkit_rule() -> bool:
    """Let the service user start its own run units, and nothing else.

    Without this an unprivileged account cannot ask the system manager to start
    anything, so every run would fail at the moment it was started. With it,
    that account can manage units matching `workbench-run@<digits>.service` and
    still nothing else — which matters more than usual here, because it is the
    account that runs model-authored shell commands.

    Returns whether the file changed, so the caller can say so.
    """
    if not POLKIT_DIR.is_dir():
        warn(f"{POLKIT_DIR} does not exist; runs will not be able to start units.")
        return False

    rendered = render_unit("workbench-run.rules.template")
    target = polkit_rule_path()
    if target.is_file() and target.read_text() == rendered:
        info("polkit rule already up to date")
        return False

    write_privileged(target, rendered, staged_as=f"{polkit_rule_name()}.staged")
    # World-readable and root-owned: polkit refuses rules it does not trust.
    run(["chmod", "0644", str(target)], privileged=True)
    run(["chown", "root:root", str(target)], privileged=True)

    info(f"wrote {target}")
    return True


def enable_deploy_timer() -> None:
    """What makes a merge to the deploy branch reach this machine without
    anyone logging in. Both roles want it — a node that cannot update itself
    is a manual step for as long as it exists."""
    run(["systemctl", "enable", "--now", "--quiet", f"{deploy_unit_name()}.timer"], privileged=True)
    info(f"deploying from '{deploy_branch()}' automatically, every {DEPLOY_INTERVAL}")


def install_service() -> None:
    """The head's units: the app, the run template's authorisation, the timer."""
    install_units()
    install_polkit_rule()

    run(["systemctl", "enable", "--quiet", service_name()], privileged=True)
    run(["systemctl", "restart", service_name()], privileged=True)
    info(f"service enabled and started as user '{service_user()}'")

    enable_deploy_timer()


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
            # urllib rather than httpx: this module has to stay importable
            # before any virtualenv exists. One GET against localhost is not
            # what httpx is for anyway.
            with urllib.request.urlopen(url, timeout=2.0) as answer:
                if answer.status == 200:
                    return None
        except urllib.error.URLError, OSError:
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


def report_outstanding() -> bool:
    """Say what a person still has to do by hand, and whether anything failed.

    Delegates to `python -m workbench.doctor`, which is also the command it
    tells people to re-run — so what the install says and what they can check
    later are the same thing, not two lists that drift.

    The reproducibility rule does not say every step can be automated — it says
    no step may be undiscoverable. Two here genuinely cannot be: joining a
    tailnet and minting the agent's credential both need a browser login
    against an account no installer can know about.

    So this is the bargain's other half. An install that finishes with work
    outstanding says so, in the terminal, with the exact commands — rather than
    leaving it to be discovered days later by a run that fails at
    authentication with nothing anywhere explaining why. Which is what
    happened, and is why this function exists.
    """
    # Run as a subprocess rather than imported, for two reasons that happen to
    # have one answer. This module has no virtualenv to import a backend from —
    # it runs before one exists. And the question is "can *the service account*
    # authenticate", which is only honestly answered by asking as that account,
    # since the credential is found through its home directory.
    #
    # Streamed, so the doctor's own output is the installer's output. There is
    # no second rendering of these findings to drift from the first.
    result = service_run([str(_venv_bin("python")), "-m", "workbench.doctor"], stream=True)
    return result.returncode == 0
