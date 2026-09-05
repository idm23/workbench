"""Configuration, resolved from the environment with repo-relative defaults.

Everything defaults to something that works from a fresh clone with no setup, so
that installing on a new machine needs no configuration step.
"""

import logging
import os
import shutil
import socket
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)


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


#: Where a local model answers, and what to ask it for. An OpenAI-compatible
#: URL rather than a vendor name, because Ollama, `llama-server` and vLLM all
#: speak it and the choice between them should not reach any code: swapping
#: one for another is a different value here, not a different backend.
#:
#: The default is loopback because that is the case needing no configuration
#: at all — a machine serving its own inference. A head reaching a worker node
#: sets this, or (once nodes are registered) learns it from one.
DEFAULT_INFERENCE_URL = "http://127.0.0.1:11434/v1"

#: The model the local backend asks for when nothing names another. A 7B coder
#: at Q4 is what fits, with room for context, in the 8 GB of VRAM this was
#: first built against — bigger models are a per-machine decision rather than a
#: default that quietly falls back to CPU.
DEFAULT_LOCAL_MODEL = "qwen2.5-coder:7b"

#: How long one request to a local model may take. Generous compared to a
#: hosted API on purpose: a MoE with its experts offloaded to system RAM can
#: spend minutes on a single long turn, and a timeout that fires mid-run costs
#: the whole run rather than the turn.
DEFAULT_INFERENCE_TIMEOUT_SECONDS = 600


def inference_base_url() -> str:
    """The OpenAI-compatible endpoint the local backend talks to."""
    configured = os.environ.get("WORKBENCH_INFERENCE_URL", "").strip()
    return (configured or DEFAULT_INFERENCE_URL).rstrip("/")


def local_model() -> str:
    """Which model the local backend asks that endpoint for."""
    return os.environ.get("WORKBENCH_LOCAL_MODEL", "").strip() or DEFAULT_LOCAL_MODEL


def inference_timeout_seconds() -> float:
    raw = os.environ.get("WORKBENCH_INFERENCE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_INFERENCE_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning("WORKBENCH_INFERENCE_TIMEOUT_SECONDS is not a number: %r", raw)
        return DEFAULT_INFERENCE_TIMEOUT_SECONDS


def sessions_dir() -> Path:
    """Where a backend keeps a conversation it has to remember itself.

    Under `data/` with everything else this machine generates, and pointedly
    *not* inside a worktree: a worktree is disposable, and a transcript stored
    in one would take the conversation with it when the task's checkout is
    removed. That is the whole difference between this and a backend whose
    sessions are keyed to the directory they ran in.
    """
    return data_dir() / "sessions"


def default_agent_backend() -> str:
    """Which agent backend to use when a project does not name one.

    Workbench is not tied to one agent. This is the machine-wide default; a
    project may override it, and every run records which backend actually ran
    it, so switching later leaves old runs correctly attributed rather than
    silently relabelled.
    """
    return os.environ.get("WORKBENCH_AGENT_BACKEND", DEFAULT_BACKEND).strip() or DEFAULT_BACKEND


#: How a run is started when nothing selects otherwise. A name, not an import:
#: nothing here knows what implements it. `local-process` is the safe default
#: because it works anywhere, including the container the fresh-install test
#: runs in, which has no systemd at all.
DEFAULT_EXECUTOR = "local-process"

#: The executor used where systemd is available and nothing overrides it. One
#: transient unit per run: its own cgroup, so a deploy restarting the app does
#: not take running agents with it, plus journald logs and resource limits per
#: run. See docs/running-agents.md.
SYSTEMD_EXECUTOR = "systemd-unit"


def systemd_available() -> bool:
    """Whether this machine can actually run a unit, not just talk about one.

    The container the fresh-install test runs in has neither, and a laptop
    checkout usually has the binary without the daemon — checking both is
    what tells those apart from a real server.
    """
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()


def default_executor() -> str:
    """Which executor to start runs with on this machine.

    Detected rather than configured by default, because the right answer is a
    property of the machine: a server with systemd should get one unit per run,
    and a container without it must still be able to run something. An explicit
    `WORKBENCH_EXECUTOR` wins over both.
    """
    configured = os.environ.get("WORKBENCH_EXECUTOR", "").strip()
    if configured:
        return configured
    if systemd_available():
        return SYSTEMD_EXECUTOR
    return DEFAULT_EXECUTOR


#: How many runs may be active at once, across every project. Runs bill a
#: subscription, so what three simultaneous agents waste is a rate-limit window
#: shared with everything else on the account — not a few dollars.
DEFAULT_MAX_CONCURRENT_RUNS = 2


def max_concurrent_runs() -> int:
    """The cap on queued-or-running runs. Zero or less means no cap.

    Deliberately small. Three taps on a phone should not start three agents,
    and the number that matters is not this machine's CPU count.
    """
    raw = os.environ.get("WORKBENCH_MAX_CONCURRENT_RUNS", "").strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENT_RUNS
    try:
        return int(raw)
    except ValueError:
        logger.warning("WORKBENCH_MAX_CONCURRENT_RUNS is not a number: %r", raw)
        return DEFAULT_MAX_CONCURRENT_RUNS


#: How long a run keeps listening for something typed into it after it has
#: gone quiet — no new input, no agent output either. Long enough that
#: reading a long reply and typing a reply of your own is not a race; short
#: enough that an abandoned run does not squat a concurrency slot or a
#: rate-limit window until the outer systemd unit timeout finally kills it.
DEFAULT_INPUT_IDLE_SECONDS = 300


def input_idle_seconds() -> int:
    raw = os.environ.get("WORKBENCH_INPUT_IDLE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_INPUT_IDLE_SECONDS
    try:
        return int(raw)
    except ValueError:
        logger.warning("WORKBENCH_INPUT_IDLE_SECONDS is not a number: %r", raw)
        return DEFAULT_INPUT_IDLE_SECONDS


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


def agent_git_identity() -> tuple[str, str]:
    """The name and email commits by this instance are authored with.

    The service account is not a person and has no identity of its own, but git
    refuses to commit without one — and the failure is not a prompt, because
    nothing here is interactive. It is an agent run dying several minutes in,
    having done the work.

    The default email is deliberately non-routable rather than a real address:
    it identifies the machine that made the commit, which is the useful thing,
    without inventing a mailbox. Override either half when commits should be
    attributed to a GitHub account instead.
    """
    name = os.environ.get("WORKBENCH_GIT_NAME", "").strip() or "Workbench"
    default_email = f"workbench@{socket.gethostname()}"
    email = os.environ.get("WORKBENCH_GIT_EMAIL", "").strip() or default_email
    return name, email


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


def service_account() -> str:
    """The dedicated unprivileged account this instance's units run as.

    Deliberately the same string as `service_name()`, rather than a name of
    its own. The unit name, the directory under `/srv`, and the account are
    one rule with one spelling, so the polkit rule's `subject.user` and the
    unit's `User=` cannot drift apart — and that pair drifting is not a
    visible bug, it is every run failing to start with an authorisation error
    that names neither of them.
    """
    return service_name()


def deployment_root() -> Path:
    """Where this instance's checkout lives once it is a deployment.

    Not a human's home, and that is forced rather than chosen. The service
    account is a different account to whoever installed it, and Ubuntu creates
    home directories mode 0750 — so a checkout under `/home/someone` is one
    the service cannot traverse at all, never mind execute a virtualenv out
    of. `/srv` is the conventional place for data a service serves, it is
    outside every user's home, and it is on the same volume as everything
    else here.

    Overridable because a laptop and the test harnesses are not deployments:
    they run the code from wherever it happens to be checked out.
    """
    override = os.environ.get("WORKBENCH_DEPLOYMENT_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path("/srv") / service_name()


def agent_home() -> Path:
    """The service account's home directory, used when creating the account.

    Kept separate from the checkout on purpose. The checkout is deployment
    state that a deploy rewrites and a relocation re-copies; this holds the
    backend's credential and its session transcripts, which are the two things
    on this machine that are on neither GitHub nor the database, and have to
    survive both.
    """
    return Path("/home") / service_account()


def deploy_unit_name() -> str:
    """The deployer's unit name, without the `.service` or `.timer`."""
    return f"{service_name()}-deploy"


def run_unit_prefix() -> str:
    """The systemd template unit runs are started from, without `@`.

    Instance-scoped like `service_name()`, and for the same reason: production
    and staging share a machine, and `workbench-run@42` started by one of them
    would otherwise be the same unit as the other's run 42 — different rows,
    different worktrees, one unit name.
    """
    suffix = instance()
    return f"workbench-{suffix}-run" if suffix else "workbench-run"


def run_unit_name(run_id: int) -> str:
    """The unit for one run. This is the handle stored on the row."""
    return f"{run_unit_prefix()}@{run_id}.service"


#: How long a run may take before systemd stops it. A backstop below the
#: backend's own turn limit rather than a replacement for it: turns bound what
#: the agent does, this bounds the process regardless of why it is stuck.
RUN_TIMEOUT_SECONDS = 3600


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
