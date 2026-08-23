#!/usr/bin/env python
"""Prove the automatic deployer works, against real systemd.

    CI=true uv run scripts/test_deploy_cycle.py
    uv run scripts/test_deploy_cycle.py --force     # on a machine you can dirty

This is the coverage nothing else provides. `test_fresh_install.py` runs in a
container, which has no systemd and therefore skips units, the timer, and the
entire deploy path; `tests/test_deploy.py` covers the git half but stops before
anything that needs root. `rebuild_and_restart()` — sync, preflight, migrate,
reinstall units, restart, health check — is otherwise never executed by a test
until it runs on the server for real.

It installs actual systemd units and requires passwordless sudo, so it refuses
to run unless CI=true or --force. GitHub's Ubuntu runners are disposable VMs
with systemd, which is what this is built for.

The checkout under test is a scratch clone whose origin is a local bare
repository, so commits can be pushed to it and the deployer exercised without
touching GitHub.
"""

import argparse
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from workbench.logs import BOLD, GREEN, RED, configure_console_logging, paint

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A distinct instance so this never collides with a real install, and so the
#: teardown below can be sure what it is removing.
INSTANCE = "citest"
SERVICE = f"workbench-{INSTANCE}"
DEPLOYER = f"{SERVICE}-deploy"
PORT = 8799

# Under home, deliberately not /tmp. The service unit sets PrivateTmp=yes,
# which gives it private /tmp and /var/tmp namespaces — a checkout under either
# is invisible from inside the unit, and systemd fails to start it with nothing
# more useful than "the control process exited with error code". This also
# matches where a real install lives.
WORKSPACE = Path.home() / "workbench-deploy-cycle"
ORIGIN = WORKSPACE / "origin.git"
CHECKOUT = WORKSPACE / "checkout"

EXCLUDED = {".venv", "data", ".git", "__pycache__", ".pytest_cache", ".ruff_cache"}


class TestFailureError(Exception):
    pass


def step(message: str) -> None:
    logger.info("\n%s", paint(BOLD, f"==> {message}"))


def ok(message: str) -> None:
    logger.info("  %s   %s", paint(GREEN, "ok"), message)


def run(
    argv: list[str], cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise TestFailureError(f"`{' '.join(argv[:4])} ...` failed:\n{detail}")
    return result


def git(*args: str, cwd: Path, check: bool = True) -> str:
    return run(["git", *args], cwd=cwd, check=check).stdout.strip()


def quiet_git(path: Path) -> None:
    """Stop git running background maintenance in a throwaway repository.

    `gc --auto` forks a detached process after some commands, and it writes
    into `.git` on its own schedule. That is fine for a repository someone
    keeps and fatal for one this script deletes a moment later: the collision
    surfaces as ENOTEMPTY on a directory that was empty when it was scanned.
    """
    run(["git", "config", "gc.auto", "0"], cwd=path)
    run(["git", "config", "maintenance.auto", "false"], cwd=path)


def remove_tree(path: Path, attempts: int = 5) -> None:
    """Delete a scratch directory, tolerating a writer that has not finished.

    Deleting scaffolding is not what this test is testing, so a failure here
    must not fail it — a suite that goes red over its own temporary files is
    one people stop believing, which is expensive when the thing it guards is
    the deploy path.

    The leftovers are logged rather than swallowed, because if this ever fires
    despite `quiet_git` above, what appeared in the directory is the whole
    diagnosis.
    """
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            if attempt == attempts - 1:
                leftovers = sorted(str(p.relative_to(path)) for p in path.rglob("*"))
                logger.warning(
                    "Could not remove %s (%s). Left behind: %s",
                    path,
                    error,
                    ", ".join(leftovers[:20]) or "nothing",
                )
                return
            time.sleep(0.2 * (attempt + 1))


def systemctl(*args: str, check: bool = True) -> str:
    return run(["sudo", "systemctl", *args], check=check).stdout.strip()


def deploy_env() -> dict[str, str]:
    """The environment that makes install.sh build the test instance."""
    return {
        **os.environ,
        "WORKBENCH_INSTANCE": INSTANCE,
        "WORKBENCH_PORT": str(PORT),
        "WORKBENCH_DEPLOY_BRANCH": "main",
    }


# --- Setup -------------------------------------------------------------------


def build_origin() -> None:
    """A bare repository holding the working tree as its first commit."""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    ORIGIN.mkdir(parents=True)
    run(["git", "init", "-q", "--bare", "-b", "main", str(ORIGIN)])
    # The receiving end runs `gc --auto` too, and this repository outlives the
    # push by the length of the whole test.
    quiet_git(ORIGIN)

    seed = WORKSPACE / "seed"
    shutil.copytree(REPO_ROOT, seed, ignore=shutil.ignore_patterns(*EXCLUDED))
    run(["git", "init", "-q", "-b", "main"], cwd=seed)
    quiet_git(seed)
    run(["git", "config", "user.email", "ci@example.com"], cwd=seed)
    run(["git", "config", "user.name", "CI"], cwd=seed)
    run(["git", "add", "-A"], cwd=seed)
    run(["git", "commit", "-qm", "working tree under test"], cwd=seed)
    run(["git", "push", "-q", str(ORIGIN), "main"], cwd=seed)
    remove_tree(seed)

    run(["git", "clone", "-q", str(ORIGIN), str(CHECKOUT)])
    run(["git", "config", "user.email", "ci@example.com"], cwd=CHECKOUT)
    run(["git", "config", "user.name", "CI"], cwd=CHECKOUT)


def commit_to_origin(message: str, edit: dict[str, str]) -> str:
    """Apply file changes on origin's main and return the new revision."""
    staging = WORKSPACE / "push"
    remove_tree(staging)
    run(["git", "clone", "-q", str(ORIGIN), str(staging)])
    quiet_git(staging)
    run(["git", "config", "user.email", "ci@example.com"], cwd=staging)
    run(["git", "config", "user.name", "CI"], cwd=staging)

    for relative, contents in edit.items():
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)

    run(["git", "add", "-A"], cwd=staging)
    run(["git", "commit", "-qm", message], cwd=staging)
    run(["git", "push", "-q", "origin", "main"], cwd=staging)
    revision = git("rev-parse", "HEAD", cwd=staging)
    # Same exposure as the seed above, and this one runs once per deploy test.
    remove_tree(staging)
    return revision


# --- Assertions --------------------------------------------------------------


def healthy(timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/healthz", timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def expect(condition: bool, description: str) -> None:
    if not condition:
        raise TestFailureError(description)
    ok(description)


def trigger_deploy() -> str:
    """Run the deployer synchronously and return its journal output."""
    systemctl("start", DEPLOYER, check=False)
    return run(["sudo", "journalctl", "-u", DEPLOYER, "-n", "60", "--no-pager"], check=False).stdout


#: Distinctive enough that finding it later means *this* row survived, rather
#: than that something happened to leave the right number of rows behind.
SEEDED_USER = "deploy-cycle-witness"


def seed_data() -> None:
    base = f"http://127.0.0.1:{PORT}"
    with httpx.Client(base_url=base, follow_redirects=True, timeout=15.0) as client:
        client.post("/users", data={"name": SEEDED_USER})
        page = client.get("/").text
        user = [link for link in page.split('"') if link.startswith("/users/")][-1]
        client.post(f"{user}/projects", data={"reference": "idm23/workbench"})


def seeded_data_present() -> bool:
    """Whether the specific row seeded earlier is still there.

    Named rather than counted. A count would pass if a row were destroyed and
    an unrelated one created — which is not a hypothetical, since anything
    driving the app over HTTP adds users.
    """
    with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=15.0) as client:
        return SEEDED_USER in client.get("/").text


# --- The test ----------------------------------------------------------------


def install() -> None:
    step("Installing the test instance with real systemd")
    result = subprocess.run(["./install.sh"], cwd=CHECKOUT, env=deploy_env())
    if result.returncode != 0:
        raise TestFailureError("install.sh failed")

    expect(systemctl("is-active", SERVICE, check=False) == "active", "service is active")
    expect(healthy(), f"service answers /healthz on {PORT}")
    expect(
        systemctl("is-enabled", f"{DEPLOYER}.timer", check=False) == "enabled",
        "deploy timer is enabled",
    )
    timers = run(["systemctl", "list-timers", "--all", f"{DEPLOYER}.timer"], check=False).stdout
    expect(DEPLOYER in timers, "deploy timer is scheduled")


def test_deploys_a_new_commit() -> None:
    step("Deploying a new commit")
    before = git("rev-parse", "HEAD", cwd=CHECKOUT)
    seed_data()
    expect(seeded_data_present(), "test data was seeded")

    revision = commit_to_origin("a deployable change", {"DEPLOYED.md": "landed\n"})
    trigger_deploy()

    expect(
        git("rev-parse", "HEAD", cwd=CHECKOUT) == revision, "checkout advanced to the new commit"
    )
    expect(git("rev-parse", "HEAD", cwd=CHECKOUT) != before, "revision actually changed")
    expect((CHECKOUT / "DEPLOYED.md").is_file(), "the new file is present in the checkout")
    expect(healthy(), "service is healthy after the restart")
    expect(seeded_data_present(), "seeded data survived the deploy")


def test_acceptance_does_not_run_where_data_matters() -> None:
    """This instance restores no snapshot, so nothing should have mutated it.

    The regression test for the bug this found: acceptance was gated on "not
    production" rather than "data is disposable", so it ran here and rewrote
    the rows the check above depends on.
    """
    step("Leaving a non-disposable instance's data alone")
    journal = run(
        ["sudo", "journalctl", "-u", DEPLOYER, "-n", "200", "--no-pager"], check=False
    ).stdout

    expect("Running staging acceptance" not in journal, "acceptance was not run")
    expect(seeded_data_present(), "seeded data is still untouched")


def test_unit_changes_reach_systemd() -> None:
    step("A changed unit template reaches /etc/systemd/system")
    template = (CHECKOUT / "deploy" / "workbench.service.template").read_text()
    marked = template.replace("[Install]", "# deploy-cycle-marker\n\n[Install]", 1)
    commit_to_origin("change the unit template", {"deploy/workbench.service.template": marked})
    trigger_deploy()

    installed = Path(f"/etc/systemd/system/{SERVICE}.service").read_text()
    expect("deploy-cycle-marker" in installed, "the edited unit was reinstalled")
    expect(healthy(), "service is healthy after a unit change")


def test_refuses_a_dirty_checkout() -> None:
    """Uncommitted work blocks the deploy, whatever the incoming commit touches.

    The commit pushed here deliberately modifies nothing that was edited
    locally. `git merge --ff-only` waves that straight through — it only
    refuses when it would overwrite a modified file — so this passing depends
    on the explicit check rather than on git's incidental protection.
    """
    step("Refusing to deploy over uncommitted work")
    (CHECKOUT / "README.md").write_text("edited on the server\n")
    commit_to_origin("a change that must not land", {"NOPE.md": "should not appear\n"})

    journal = trigger_deploy()

    expect("Deploy failed" in journal, "the deploy reported failure")
    expect("checking for local changes" in journal, "it named the uncommitted work")
    expect(not (CHECKOUT / "NOPE.md").exists(), "the unrelated change was not applied")
    expect(
        (CHECKOUT / "README.md").read_text() == "edited on the server\n",
        "local edits were left alone",
    )
    expect(healthy(), "service is untouched and still healthy")

    git("checkout", "--", "README.md", cwd=CHECKOUT)


def test_crash_on_boot_is_caught_before_the_restart() -> None:
    """The regression test for a deploy that reported success into a dead service."""
    step("Catching code that cannot even be imported")
    broken = "this is not valid python\n"
    commit_to_origin("break the app at import time", {"src/workbench/app.py": broken})

    journal = trigger_deploy()

    expect("Deploy failed" in journal, "the deploy reported failure rather than success")
    expect("importing the new code" in journal, "preflight named the failing step")
    expect(healthy(), "the running service was never restarted into broken code")


def diagnose() -> None:
    """Dump everything needed to understand a failure, before tearing it down.

    Both units, because which one broke is exactly what is unclear when this
    fails: a service that will not start and a deployer that will not deploy
    look the same from the outside, and the journal of the one that never ran
    is empty and misleading.
    """
    for unit in (SERVICE, DEPLOYER):
        logger.error("\n--- systemctl status %s ---", unit)
        logger.error("%s", run(["systemctl", "status", "--no-pager", unit], check=False).stdout)
        logger.error("--- journalctl -u %s ---", unit)
        logger.error(
            "%s",
            run(["sudo", "journalctl", "-u", unit, "-n", "80", "--no-pager"], check=False).stdout,
        )

    logger.error("--- rendered %s.service ---", SERVICE)
    unit_file = Path(f"/etc/systemd/system/{SERVICE}.service")
    if unit_file.is_file():
        logger.error("%s", unit_file.read_text())

    logger.error("--- checkout ---")
    logger.error("%s", run(["ls", "-la", str(CHECKOUT)], check=False).stdout)


def teardown() -> None:
    step("Removing the test instance")
    systemctl("disable", "--now", f"{DEPLOYER}.timer", check=False)
    systemctl("disable", "--now", SERVICE, check=False)
    systemctl("stop", DEPLOYER, check=False)
    for unit in (f"{SERVICE}.service", f"{DEPLOYER}.service", f"{DEPLOYER}.timer"):
        run(["sudo", "rm", "-f", f"/etc/systemd/system/{unit}"], check=False)
    systemctl("daemon-reload", check=False)
    shutil.rmtree(WORKSPACE, ignore_errors=True)


def main() -> int:
    configure_console_logging()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run outside CI, accepting that this installs systemd units and needs sudo",
    )
    arguments = parser.parse_args()

    if os.environ.get("CI") != "true" and not arguments.force:
        logger.error(
            "This installs real systemd units and requires passwordless sudo.\n"
            "It is meant for a disposable CI runner. Pass --force to run it anyway."
        )
        return 2

    if shutil.which("systemctl") is None or not Path("/run/systemd/system").is_dir():
        logger.error("systemd is not running here; this test has nothing to exercise.")
        return 2

    shutil.rmtree(WORKSPACE, ignore_errors=True)
    try:
        build_origin()
        install()
        test_deploys_a_new_commit()
        test_acceptance_does_not_run_where_data_matters()
        test_unit_changes_reach_systemd()
        test_refuses_a_dirty_checkout()
        test_crash_on_boot_is_caught_before_the_restart()
    except TestFailureError as error:
        logger.error("\n%s %s\n", paint(RED, "FAIL"), error)
        diagnose()
        teardown()
        return 1

    teardown()
    logger.info("\n%s\n", paint(BOLD, "Deploy cycle works, on real systemd."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
