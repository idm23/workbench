"""Configuration, resolved from the environment with repo-relative defaults.

Everything defaults to something that works from a fresh clone with no setup, so
that installing on a new machine needs no configuration step.
"""

import os
from collections.abc import Mapping
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


#: The agent backend used when nothing selects another one. A name, not an
#: import: nothing here knows what implements it, or that Claude exists.
DEFAULT_BACKEND = "claude"


def data_dir() -> Path:
    """Everything this machine generates, as opposed to everything it clones.

    Derived from the database path rather than from the repository root, so
    pointing WORKBENCH_DB elsewhere takes the clones and worktrees with it
    instead of leaving them beside a database that has moved.
    """
    return database_path().parent


def repos_dir() -> Path:
    """Where a project's repository is cloned.

    Inside `data/` for the same reason the database is: it is generated, it is
    gitignored, and a fresh clone of Workbench onto another machine should not
    inherit a path that has to be created and remembered.
    """
    return data_dir() / "repos"


def worktrees_dir() -> Path:
    """Where each task's worktree is created.

    Disposable by design — a worktree can be deleted and remade from the branch
    it points at, which is why nothing irreplaceable is ever kept here.
    """
    return data_dir() / "worktrees"


def default_agent_backend() -> str:
    """Which agent backend to use when a project does not name one.

    Workbench is not tied to one agent. This is the machine-wide default; a
    project may override it, and every run records which backend actually ran
    it, so switching later leaves old runs correctly attributed rather than
    silently relabelled.
    """
    return os.environ.get("WORKBENCH_AGENT_BACKEND", DEFAULT_BACKEND).strip() or DEFAULT_BACKEND


#: Credential variables that switch a backend from a subscription to
#: metered API billing. Named here rather than inside a backend because the
#: choice is Workbench's, and the next backend will have its own spelling of
#: the same idea to add to this list.
API_CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def billing_mode() -> str:
    """Whether runs bill a subscription or a metered API key.

    Defaults to `subscription`, which is a decision rather than a fallback.
    The failure it prevents is silent: an `ANTHROPIC_API_KEY` exported into a
    shell, inherited by a service, or added to `/etc/workbench/env` for some
    unrelated tool would switch every run onto per-token billing without
    changing anything visible in Workbench. Nobody would notice until a bill
    arrived.

    So the runner strips those variables rather than merely declining to set
    them, and someone who genuinely wants metered billing says so out loud with
    `WORKBENCH_BILLING=api`.
    """
    return os.environ.get("WORKBENCH_BILLING", "subscription").strip().lower() or "subscription"


def bills_subscription() -> bool:
    return billing_mode() != "api"


def agent_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment an agent process should run with.

    A copy, and pure, so the decision above can be tested without a subprocess
    or a mutated interpreter. The runner applies it to itself once at startup,
    which is the single place where every backend inherits from.

    Note what is *not* removed: the agent still needs a credential, and under a
    subscription that is the OAuth token in the service user's home directory.
    This makes the account concrete rather than ambient — the home directory of
    the user the unit runs as is the thing that decides who pays.
    """
    env = dict(os.environ if base is None else base)
    if bills_subscription():
        for name in API_CREDENTIAL_VARS:
            env.pop(name, None)
    return env


def deploy_branch() -> str:
    """The branch the automatic deployer follows.

    A checkout sitting on anything else is left alone, so working on the server
    by hand does not get interrupted by a deploy.
    """
    return os.environ.get("WORKBENCH_DEPLOY_BRANCH", "main")


def instance() -> str:
    """Which install this is: empty for production, `staging` for staging.

    Two installs coexist on one machine, and almost everything that separates
    them is already free — a second checkout gets its own `data/` and venv
    because those are repo-relative, and the port is configurable. Unit names
    are the exception: without a suffix the second install would overwrite the
    first's systemd units and then restart it on every deploy.
    """
    return os.environ.get("WORKBENCH_INSTANCE", "").strip().strip("-")


def service_name() -> str:
    """The systemd unit name for this instance, without the `.service`."""
    suffix = instance()
    return f"workbench-{suffix}" if suffix else "workbench"


def deploy_unit_name() -> str:
    """The deployer's unit name, without the `.service` or `.timer`."""
    return f"{service_name()}-deploy"


def github_token() -> str | None:
    """A fine-grained PAT, if one has been configured.

    Optional by design: without it the app still manages tasks and the deployer
    still fetches a public repository. Only reporting the staging result back
    to GitHub needs it — which is what branch protection on `main` waits for,
    so a missing token stalls promotion rather than breaking anything.
    """
    token = os.environ.get("WORKBENCH_GITHUB_TOKEN", "").strip()
    return token or None


def restore_from() -> Path | None:
    """Another instance's database to copy over this one before migrating.

    Set on staging and never on production. This is what makes staging a real
    test of a migration rather than a rehearsal against empty tables.
    """
    configured = os.environ.get("WORKBENCH_RESTORE_FROM", "").strip()
    if not configured:
        return None
    return Path(configured).expanduser().resolve()


def host() -> str:
    return os.environ.get("WORKBENCH_HOST", "127.0.0.1")


def port() -> int:
    return int(os.environ.get("WORKBENCH_PORT", "8787"))
