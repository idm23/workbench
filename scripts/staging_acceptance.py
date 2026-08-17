#!/usr/bin/env python
"""Exercise the staging install, and report the verdict back to GitHub.

    uv run scripts/staging_acceptance.py
    uv run scripts/staging_acceptance.py --no-report   # local, no commit status

Run on the server after a staging deploy. Staging holds a snapshot of
production's database, migrated by the deploy that just finished, so this is
the only place a revision meets real rows before the machine you depend on
does.

The result is posted as a commit status on the revision staging is sitting at.
That is an **outbound** call, which is the whole reason this design works: the
server has no public ingress, so GitHub cannot ask how staging went — the
server has to tell it. The same constraint is why deploys poll.

A status named `staging-acceptance` is what branch protection on `main` waits
for, so a commit that has never run here cannot be promoted.
"""

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from workbench.config import github_token, host, instance, port, repo_root, service_name
from workbench.db import get_session_factory
from workbench.logs import BOLD, GREEN, RED, YELLOW, configure_console_logging, paint
from workbench.models import Project, Run, Task, User

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
STATUS_CONTEXT = "staging-acceptance"
REPO_SLUG = "idm23/workbench"


@dataclass
class Results:
    passed: int = 0
    failures: list[str] = field(default_factory=list)

    def check(self, description: str, condition: bool, detail: str = "") -> bool:
        if condition:
            logger.info("  %s   %s", paint(GREEN, "ok"), description)
            self.passed += 1
            return True
        logger.error("  %s %s%s", paint(RED, "FAIL"), description, f" — {detail}" if detail else "")
        self.failures.append(description)
        return False

    def note(self, message: str) -> None:
        logger.warning("  %s %s", paint(YELLOW, "note"), message)


def revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def check_service(results: Results) -> None:
    """The app is up, and up because of the deploy rather than a restart loop."""
    base = f"http://{host()}:{port()}"
    try:
        healthz = httpx.get(f"{base}/healthz", timeout=5.0)
        results.check("service answers /healthz", healthz.status_code == 200)
    except httpx.HTTPError as error:
        results.check("service answers /healthz", False, str(error))
        return

    # NRestarts climbing means the unit is crash-looping, which Restart=always
    # will happily hide behind a healthy-looking answer between attempts.
    restarts = subprocess.run(
        ["systemctl", "show", "-p", "NRestarts", "--value", service_name()],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    results.check(
        "service is not restart-looping",
        restarts in ("", "0"),
        f"NRestarts={restarts}",
    )


def check_restored_data(results: Results) -> None:
    """The production snapshot survived being migrated.

    This is the point of staging. Counting rows is not much of a test on its
    own, but these rows arrived through a real migration of real production
    data, which is the step that cannot be rehearsed anywhere else.
    """
    session = get_session_factory()()
    try:
        users = session.query(User).count()
        projects = session.query(Project).count()
        tasks = session.query(Task).count()
        runs = session.query(Run).count()
    finally:
        session.close()

    logger.info(
        "    restored: %d users, %d projects, %d tasks, %d runs", users, projects, tasks, runs
    )
    results.check("the restored database has rows in it", users > 0)

    if projects == 0:
        results.note("no projects in the snapshot; the render check below proves less than usual")


def check_pages_render(results: Results) -> None:
    """Every page real data can reach still renders after migrating.

    A migration that leaves a column the templates read as NULL produces a 500
    on exactly one record's page and none of the others, which no row count
    would catch.

    Pages are discovered by following links rather than built from route
    patterns, so this checks what the deployed revision actually serves. A
    route that does not exist yet is simply never linked to and never probed,
    and starts being covered on its own the commit it ships.
    """
    base = f"http://{host()}:{port()}"
    session = get_session_factory()()
    try:
        user_ids = [row.id for row in session.query(User).all()]
    finally:
        session.close()

    def status_of(client: httpx.Client, path: str) -> int:
        """A connection error is a failing page, not an exception.

        Every check here has to produce a verdict: this script's whole job is
        to report one, and a traceback would leave the pull request with no
        status at all rather than a red one.
        """
        try:
            return client.get(path).status_code
        except httpx.HTTPError:
            return 0

    with httpx.Client(base_url=base, timeout=15.0, follow_redirects=True) as client:
        results.check("index renders", status_of(client, "/") == 200)

        broken = [i for i in user_ids if status_of(client, f"/users/{i}") != 200]
        results.check("every user page renders", not broken, f"failed: {broken}")

        linked = sorted(_linked_paths(client, [f"/users/{i}" for i in user_ids]))
        broken = [path for path in linked if status_of(client, path) != 200]
        results.check(
            f"every linked page renders ({len(linked)} found)",
            not broken,
            f"failed: {broken}",
        )


def _linked_paths(client: httpx.Client, pages: list[str]) -> set[str]:
    """Internal links found on the given pages, as a user would reach them."""
    found: set[str] = set()
    for page in pages:
        try:
            html = client.get(page).text
        except httpx.HTTPError:
            continue
        for fragment in html.split('href="')[1:]:
            path = fragment.split('"', 1)[0]
            # Relative links only: external ones are GitHub's problem.
            if path.startswith("/") and not path.startswith("//"):
                found.add(path)
    return found


def check_schema_at_head(results: Results) -> None:
    """Staging is running the migrations this commit ships, not older ones."""
    alembic = repo_root() / ".venv" / "bin" / "alembic"
    result = subprocess.run(
        [str(alembic), "check"], cwd=repo_root(), capture_output=True, text=True, check=False
    )
    results.check(
        "schema matches the models",
        result.returncode == 0,
        (result.stderr or result.stdout).strip()[:200],
    )


def report(sha: str, success: bool, summary: str) -> bool:
    """Post the commit status. Returns whether GitHub accepted it."""
    token = github_token()
    if token is None:
        logger.error(
            "No WORKBENCH_GITHUB_TOKEN, so the result cannot be reported. "
            "The status is what branch protection on main waits for."
        )
        return False

    try:
        response = httpx.post(
            f"{API_ROOT}/repos/{REPO_SLUG}/statuses/{sha}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "workbench",
            },
            json={
                "state": "success" if success else "failure",
                "context": STATUS_CONTEXT,
                "description": summary[:140],
            },
            timeout=20.0,
        )
    except httpx.HTTPError as error:
        logger.error("Could not reach GitHub to report the result: %s", error)
        return False

    if response.status_code != 201:
        logger.error("GitHub returned %s posting the status.", response.status_code)
        return False
    return True


def main() -> int:
    configure_console_logging()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="run the checks without posting a commit status",
    )
    arguments = parser.parse_args()

    if instance() == "":
        logger.error(
            "This is the production instance. Acceptance runs against staging, which\n"
            "holds a disposable copy of this database — running it here would prove\n"
            "nothing and report a status for the wrong install."
        )
        return 2

    sha = revision()
    logger.info("%s", paint(BOLD, f"Staging acceptance for {sha[:7]} on :{port()}"))

    results = Results()
    try:
        check_service(results)
        check_schema_at_head(results)
        check_restored_data(results)
        check_pages_render(results)

        # The HTTP behaviour itself is already covered by smoke_test.py;
        # running it here rather than reimplementing keeps one definition of
        # "the app works".
        smoke = subprocess.run(
            [
                str(repo_root() / ".venv" / "bin" / "python"),
                str(Path(__file__).parent / "smoke_test.py"),
                "--base-url",
                f"http://{host()}:{port()}",
            ],
            check=False,
        )
        results.check("smoke test passes against staging", smoke.returncode == 0)
    except Exception as error:
        # Broad on purpose, and the reason is the whole design: an unhandled
        # exception here would leave the pull request with no status rather
        # than a red one, and "no status" is indistinguishable from "staging
        # has not run yet". A crash is a failure and has to be reported as one.
        logger.exception("Acceptance crashed")
        results.check(f"acceptance completed without crashing ({type(error).__name__})", False)

    success = not results.failures
    summary = (
        f"{results.passed} checks passed"
        if success
        else f"{len(results.failures)} failed: {', '.join(results.failures)[:100]}"
    )
    logger.info("\n%s", paint(BOLD if success else RED, summary))

    if arguments.no_report:
        return 0 if success else 1

    if not sha:
        logger.error("Could not determine the revision; not reporting.")
        return 1
    if not report(sha, success, summary):
        return 1

    logger.info("Reported %s to GitHub as %s.", STATUS_CONTEXT, "success" if success else "failure")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
