#!/usr/bin/env python
"""Verify a running Workbench actually works, end to end over HTTP.

    uv run scripts/smoke_test.py [--base-url URL]

Safe to run against an install with real data in it: everything it creates is
namespaced with a timestamp.

Uses httpx rather than curl partly for readability and partly because httpx
switches a 303 redirect to GET automatically. Doing that wrong with curl (by
passing -X POST, which forces the method through the redirect) silently turns
every check into a 405 and reports false failures.
"""

import argparse
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from workbench.logs import GREEN, RED, YELLOW, configure_console_logging, paint

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8787"
STARTUP_TIMEOUT_SECONDS = 20.0


@dataclass
class Results:
    passed: int = 0
    failures: list[str] = field(default_factory=list)

    def record_pass(self, description: str) -> None:
        logger.info("  %s   %s", paint(GREEN, "ok"), description)
        self.passed += 1

    def check(self, description: str, expected: str, actual: str) -> bool:
        if expected in actual:
            self.record_pass(description)
            return True
        condensed = " ".join(actual.split())[:200]
        logger.error("  %s %s", paint(RED, "FAIL"), description)
        logger.error("       wanted: %s", expected)
        logger.error("       got:    %s", condensed)
        self.failures.append(description)
        return False

    def check_absent(self, description: str, unwanted: str, actual: str) -> bool:
        """The mirror of check(), for things that must have disappeared.

        Deletion is only verifiable this way: the page rendering successfully
        proves nothing if the row is still on it.
        """
        if unwanted not in actual:
            self.record_pass(description)
            return True
        logger.error("  %s %s", paint(RED, "FAIL"), description)
        logger.error("       still present: %s", unwanted)
        self.failures.append(description)
        return False

    def note(self, message: str) -> None:
        logger.warning("  %s %s", paint(YELLOW, "note"), message)


def wait_until_healthy(client: httpx.Client) -> None:
    """The service may still be starting, especially right after an install."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if client.get("/healthz", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)


def run(base_url: str) -> Results:
    results = Results()
    user_name = f"smoketest-{int(time.time())}"

    with httpx.Client(base_url=base_url, follow_redirects=True, timeout=15.0) as client:
        wait_until_healthy(client)

        results.check("health endpoint answers", '"status":"ok"', client.get("/healthz").text)
        results.check("user list renders", "Workbench", client.get("/").text)

        client.post("/users", data={"name": user_name})
        results.check("created user appears", user_name, client.get("/").text)

        results.check(
            "duplicate user rejected",
            "already a user called",
            client.post("/users", data={"name": user_name}).text,
        )

        # Find the user we just made, so the run is independent of any existing
        # data on the machine.
        links = re.findall(r"/users/\d+", client.get("/").text)
        if not links:
            results.check("found the new user's page", "a /users/<id> link", "none in the page")
            return results
        results.record_pass("found the new user's page")
        projects_url = f"{links[-1]}/projects"

        added = client.post(projects_url, data={"reference": "idm23/workbench"}).text
        results.check("project added", "idm23/workbench", added)
        if "without details" in added:
            # Unauthenticated GitHub allows 60 requests an hour, and the app
            # deliberately still saves the project. Exercising that path is a
            # pass, not a failure.
            results.note("GitHub metadata unavailable (rate limit?) — degrade path worked")

        results.check(
            "duplicate project rejected",
            "already has idm23/workbench",
            client.post(
                projects_url, data={"reference": "git@github.com:idm23/workbench.git"}
            ).text,
        )
        results.check(
            "bad reference rejected",
            "is not a GitHub repository",
            client.post(projects_url, data={"reference": "not a repo"}).text,
        )
        results.check(
            "unknown user is a 404",
            "404",
            str(client.get("/users/99999999").status_code),
        )

        _check_tasks(client, results)

    return results


def _check_tasks(client: httpx.Client, results: Results) -> None:
    """Task CRUD, on the project just added.

    Deliberately stops short of starting a run. A run needs credentials a fresh
    install does not have, and this script has to pass on a clean machine — so
    what it verifies instead is that refusing to run is a readable message
    rather than a traceback.
    """
    user_links = re.findall(r"/users/\d+", client.get("/").text)
    project_links = re.findall(r"/projects/\d+", client.get(user_links[-1]).text)
    if not project_links:
        results.check("found a project to add tasks to", "a /projects/<id> link", "none")
        return
    results.record_pass("found a project to add tasks to")

    project_url = project_links[-1]
    tasks_url = f"{project_url}/tasks"

    page = client.post(tasks_url, data={"title": "Smoke test task", "body": "detail"}).text
    results.check("task added", "Smoke test task", page)

    parent_ids = re.findall(r"/tasks/(\d+)/status", page)
    if not parent_ids:
        results.check("found the new task", "a /tasks/<id> control", "none in the page")
        return
    parent_id = parent_ids[-1]

    page = client.post(tasks_url, data={"title": "Smoke test subtask", "parent_id": parent_id}).text
    results.check("subtask nests under its parent", "Smoke test subtask", page)
    results.check("parent shows no children done yet", "0/1", page)

    results.check(
        "a parent task cannot be run",
        "has sub-tasks",
        client.post(f"/tasks/{parent_id}/runs").text,
    )

    child_ids = [i for i in re.findall(r"/tasks/(\d+)/status", page) if i != parent_id]
    if not child_ids:
        results.check("found the subtask", "a second /tasks/<id> control", "none")
        return

    # Before completing it, while it is still a runnable leaf: without a clone
    # there is nowhere to make a worktree, which is the state a fresh install
    # is always in.
    results.check(
        "running an uncloned project is refused clearly",
        "not been cloned",
        client.post(f"/tasks/{child_ids[-1]}/runs").text,
    )

    results.check(
        "completing a subtask updates the parent's progress",
        "1/1",
        client.post(f"/tasks/{child_ids[-1]}/status", data={"new_status": "done"}).text,
    )

    results.check(
        "a completed task is not runnable",
        "Reopen it",
        client.post(f"/tasks/{child_ids[-1]}/runs").text,
    )

    deleted = client.post(f"/tasks/{parent_id}/delete").text
    results.check_absent("deleting a parent removes its children", "Smoke test subtask", deleted)


def main() -> int:
    configure_console_logging()

    # __doc__ is None under `python -OO`; argparse accepts None, so pass it
    # through rather than indexing into it.
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    arguments = parser.parse_args()

    logger.info("Smoke testing %s\n", arguments.base_url)
    results = run(arguments.base_url)
    logger.info("\npassed=%d failed=%d", results.passed, len(results.failures))
    return 1 if results.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
