"""Configuration, resolved from the environment with repo-relative defaults.

Everything defaults to something that works from a fresh clone with no setup, so
that installing on a new machine needs no configuration step.
"""

import os
from pathlib import Path


def repo_root() -> Path:
    """The project root, found by walking up for pyproject.toml.

    Walking up rather than counting `parents[n]` keeps this correct whether the
    package is imported from the source tree or from an editable install.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


def database_path() -> Path:
    """Where the SQLite file lives.

    Defaults inside the repo rather than somewhere like /var/lib so that a clone
    is self-contained: no directory to create out of band, nothing to remember.
    `data/` is gitignored, so pulling never touches it.
    """
    override = os.environ.get("WORKBENCH_DB")
    if override:
        return Path(override).expanduser().resolve()
    return repo_root() / "data" / "workbench.db"


def database_url() -> str:
    return f"sqlite+pysqlite:///{database_path()}"


def ensure_data_dir() -> Path:
    """Create the database's parent directory. Safe to call repeatedly."""
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    """The directory holding everything this machine generates.

    Derived from the database path so that pointing WORKBENCH_DB elsewhere
    moves the clones and worktrees with it, rather than leaving them behind in
    the repo pointing at a database that is no longer there.
    """
    return database_path().parent


def repos_dir() -> Path:
    """Where project repositories are cloned."""
    return data_dir() / "repos"


def worktrees_dir() -> Path:
    """Where per-task worktrees are created."""
    return data_dir() / "worktrees"


def github_token() -> str | None:
    """A fine-grained PAT, if one has been configured.

    Optional by design: without it the app still reads public repositories and
    manages tasks, and only pushing and opening pull requests are unavailable.
    That keeps a fresh install working with no secret to supply.
    """
    token = os.environ.get("WORKBENCH_GITHUB_TOKEN", "").strip()
    return token or None


def max_concurrent_runs() -> int:
    """How many agent runs may be in flight at once.

    Low by default. Each run is an agent executing builds on a home server, and
    the interface is a phone where three taps is an easy accident.
    """
    return int(os.environ.get("WORKBENCH_MAX_CONCURRENT_RUNS", "2"))


def host() -> str:
    return os.environ.get("WORKBENCH_HOST", "127.0.0.1")


def port() -> int:
    return int(os.environ.get("WORKBENCH_PORT", "8787"))
