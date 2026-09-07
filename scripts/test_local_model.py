#!/usr/bin/env python
"""Find out whether a local model can actually drive a Workbench run.

    uv run scripts/test_local_model.py --model qwen3:8b
    uv run scripts/test_local_model.py --model gpt-oss:20b --url http://node:11434/v1

Everything else in `scripts/` tests Workbench. This tests a *model*, because
the answer turns out not to follow from its size or its benchmark scores: the
first 7B tried here wrote every tool call as prose and never touched the
tool-call channel at all, and the first 8B used the channel correctly and then
spent its whole turn budget reasoning about what to do instead of doing it.
Neither is visible from a model card.

So this gives one small, unambiguous task to a real endpoint and reports what
came back: did it use its tools, did it change the file, did it commit, did it
say what it had done. Everything is temporary — its own database, its own git
repository, its own worktree — and nothing touches an existing install.
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from workbench.logs import BOLD, GREEN, RED, YELLOW, configure_console_logging, paint

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_PORT = 8904
APP_TIMEOUT_SECONDS = 30.0

#: Deliberately small, and deliberately not trivial. It needs one look at a
#: file, one edit that has to match text the model has actually read, and one
#: shell command — which is the shortest path that exercises the three things a
#: run is made of. A model that cannot do this cannot do a real task.
TASK_TITLE = "Add a greet function"
TASK_BODY = (
    "Add a function `greet(name)` to app.py that returns the string "
    "'Hello, <name>!'. Call it from main() instead of printing 'hello'. "
    "Then commit the change."
)

SEED_APP = '"""The demo application."""\n\n\ndef main() -> None:\n    print("hello")\n'


def step(message: str) -> None:
    logger.info("\n%s", paint(BOLD, f"==> {message}"))


def git(where: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=where, check=True, capture_output=True)


def seed_repository(work: Path) -> None:
    """A real repository with a real remote, so the run's push path behaves.

    A bare repo beside the clone rather than a mock: `git fetch` and
    `git worktree add` are the same operations here as on the server, and the
    push at the end fails the same honest way it would against a GitHub remote
    nobody gave this machine a key for.
    """
    origin = work / "origin.git"
    seed = work / "seed"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    seed.mkdir(parents=True)
    git(seed, "init", "-b", "main")
    git(seed, "config", "user.email", "seed@example.com")
    git(seed, "config", "user.name", "Seed")
    (seed / "README.md").write_text("# Demo project\n\nA project used to try one model.\n")
    (seed / "app.py").write_text(SEED_APP)
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "Initial commit")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main")

    clone = work / "data" / "repos" / "demo-project"
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    git(clone, "config", "user.email", "workbench@example.com")
    git(clone, "config", "user.name", "Workbench")


def seed_rows() -> int:
    from workbench.database.db import session_scope
    from workbench.database.models import Project, Run, RunPhase, RunStatus, Task, User

    with session_scope() as db:
        user = User(name="model-test")
        project = Project(
            user=user,
            owner="demo",
            repo="project",
            github_url="https://github.com/demo/project",
            default_branch="main",
            agent_backend="local",
        )
        task = Task(project=project, title=TASK_TITLE, body=TASK_BODY)
        run = Run(task=task, phase=RunPhase.EXECUTE, status=RunStatus.QUEUED, backend="local")
        db.add_all([user, project, task, run])
        db.commit()
        return run.id


def start_app(env: dict[str, str]) -> subprocess.Popen:
    """The app, because `report_outcome` is an HTTP call to it.

    Without one running, a model that does everything right still gets an error
    from the one tool that ends a run — which would make this harness fail
    models for a reason that has nothing to do with them.
    """
    import httpx

    app = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "workbench.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(APP_PORT),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + APP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{APP_PORT}/", timeout=1)
            return app
        except httpx.HTTPError:
            time.sleep(0.5)
    return app


def report(run_id: int, seconds: float) -> bool:
    """What the model did, and whether it amounts to having done the task."""
    from workbench.database.db import session_scope
    from workbench.database.models import Run, RunEvent, RunEventKind

    with session_scope() as db:
        run = db.get(Run, run_id)
        assert run is not None
        events = db.query(RunEvent).filter(RunEvent.run_id == run_id).order_by(RunEvent.seq).all()
        tools = [
            (event.payload or {}).get("name")
            for event in events
            if event.kind is RunEventKind.TOOL_USE
        ]
        recovered = sum(
            1
            for event in events
            if event.kind is RunEventKind.NOTICE
            and "as text" in str((event.payload or {}).get("text", ""))
        )
        worktree = Path(run.task.worktree_path) if run.task and run.task.worktree_path else None
        committed = False
        greets = False
        if worktree is not None:
            log = subprocess.run(
                ["git", "log", "--oneline", "main..HEAD"],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=False,
            )
            committed = bool(log.stdout.strip())
            app_py = worktree / "app.py"
            greets = app_py.is_file() and "def greet" in app_py.read_text()

        step("What happened")
        logger.info("    model            : %s", run.model)
        logger.info("    wall clock       : %.0fs over %s turn(s)", seconds, run.num_turns)
        logger.info("    tool calls       : %s", ", ".join(t for t in tools if t) or "none")
        logger.info("    events recorded  : %s", len(events))
        if recovered:
            logger.info(
                "    %s      : %s turn(s) wrote tool calls as prose rather than calling them",
                paint(YELLOW, "recovered"),
                recovered,
            )
        logger.info("    run status       : %s", run.status.value)
        logger.info("    task status      : %s", run.task.status.value if run.task else "-")
        logger.info("    summary          : %s", (run.summary or "").strip()[:100] or "(none)")

        checks = (
            ("used its tools at all", bool(tools)),
            ("changed the file it was asked to change", greets),
            ("committed the change", committed),
            ("reported an outcome", run.agent_outcome is not None),
        )
        step("Verdict")
        for description, passed in checks:
            mark = paint(GREEN, "ok  ") if passed else paint(RED, "no  ")
            logger.info("    %s %s", mark, description)
        return all(passed for _, passed in checks)


def main() -> int:
    configure_console_logging()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", required=True, help="the model to ask for, e.g. qwen3:8b")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:11434/v1",
        help="an OpenAI-compatible endpoint (default: this machine's Ollama)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the temporary worktree behind to look at"
    )
    arguments = parser.parse_args()

    work = Path(os.environ.get("TMPDIR", "/tmp")) / f"workbench-model-test-{os.getpid()}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    env = dict(os.environ)
    env.update(
        WORKBENCH_DB=str(work / "data" / "workbench.db"),
        WORKBENCH_AGENT_BACKEND="local",
        WORKBENCH_INFERENCE_URL=arguments.url,
        WORKBENCH_LOCAL_MODEL=arguments.model,
        WORKBENCH_PORT=str(APP_PORT),
        WORKBENCH_EXECUTOR="local-process",
        WORKBENCH_GIT_NAME="Workbench",
        WORKBENCH_GIT_EMAIL="workbench@example.com",
        # Fail fast rather than hanging on a key this scratch repo has not got.
        GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=5",
        PYTHONPATH=str(REPO_ROOT / "src"),
    )
    os.environ.update(env)

    app = None
    try:
        step(f"Setting up a scratch project in {work}")
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
        )
        seed_repository(work)
        run_id = seed_rows()

        step(f"Asking {arguments.model} at {arguments.url} to do one small task")
        logger.info("    %s", TASK_BODY)
        app = start_app(env)

        started = time.monotonic()
        subprocess.run(
            [sys.executable, "-m", "workbench.runs.runner", str(run_id)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        seconds = time.monotonic() - started

        passed = report(run_id, seconds)
    finally:
        if app is not None:
            app.terminate()
        if arguments.keep:
            logger.info("\nLeft behind: %s", work)
        else:
            shutil.rmtree(work, ignore_errors=True)

    if passed:
        logger.info("\n%s\n", paint(GREEN, f"{arguments.model} can drive a run."))
        return 0
    logger.info(
        "\n%s\n",
        paint(RED, f"{arguments.model} did not complete the task. See above for how far it got."),
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
