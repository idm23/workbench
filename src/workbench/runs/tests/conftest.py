"""Fixtures shared by the run store's tests and the runner's.

The runner reaches for the process-wide engine through `session_scope`, so the
cached engine has to be cleared as well as redirected — otherwise the second
test in a session quietly writes to the first one's database.
"""

import subprocess

import pytest

from workbench.database.db import get_engine, get_session_factory
from workbench.database.models import Base, Project, RunPhase, Task, User
from workbench.git.worktrees import clone_path_for
from workbench.runs.store import create_run


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the database, clones, and worktrees at a temporary tree."""
    db_path = tmp_path / "data" / "workbench.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WORKBENCH_DB", str(db_path))
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield db_path.parent
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def db(data_dir):
    """The session the runner would actually get, not a lookalike.

    Through `get_session_factory()`, which sets `expire_on_commit=False`, and
    not a bare `Session(engine)` — whose default is the opposite. That
    difference is not cosmetic: with auto-expiry on, every `db.commit()`
    silently re-reads the row, so a test cannot see a value another process
    changed going stale. It masked a real bug, where the runner missed every
    outcome the agent reported through the API because those are written by
    the web process and this one never looked again.
    """
    engine = get_engine()
    Base.metadata.create_all(engine)
    with get_session_factory()() as session:
        yield session


@pytest.fixture
def task(db) -> Task:
    project = Project(
        user=User(name="ian"),
        owner="idm23",
        repo="workbench",
        github_url="https://github.com/idm23/workbench",
        default_branch="main",
    )
    task = Task(project=project, title="Add route tests", body="Cover the HTML routes.")
    db.add(task)
    db.commit()
    return task


@pytest.fixture
def checkout(task):
    """A real clone where `local_checkout` will actually find it."""
    path = clone_path_for(task.project.owner, task.project.repo)
    path.mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(("git", *args), cwd=path, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    git("add", ".")
    git("commit", "-m", "first")
    return path


@pytest.fixture
def run(db, task):
    return create_run(db, task, RunPhase.EXECUTE, backend="fake")
