"""Git operations: cloning a project, and giving each task its own worktree.

One worktree per task, not per run. A planning run and the execute run that
follows it are two attempts at one piece of work, and the second resumes the
first's conversation through an opaque token the backend scopes to the
directory it ran in. Per-run worktrees would invalidate that token every time.

Every function returns a result object instead of raising, so callers
discriminate with `isinstance` and a type checker can prove they handled the
failure. Git is full of expected failures (dirty tree, missing branch, no
network) and none of them deserve a traceback.
"""

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from workbench.config import repos_dir, worktrees_dir

logger = logging.getLogger(__name__)

#: Git operations that touch the network. Generous, because a first clone of a
#: large repository over a home connection is legitimately slow.
NETWORK_TIMEOUT_SECONDS = 600

#: Everything local. A worktree add or a diff should be near-instant; a minute
#: means something is wrong.
LOCAL_TIMEOUT_SECONDS = 60

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class GitOk:
    """The command succeeded."""

    stdout: str


@dataclass(frozen=True)
class GitFailed:
    """The command ran and returned non-zero, or could not be run at all."""

    message: str
    stderr: str = ""


type GitResult = GitOk | GitFailed


def _run_git(
    args: list[str], cwd: Path | None = None, timeout: int = LOCAL_TIMEOUT_SECONDS
) -> GitResult:
    """Run git, capturing both streams and never raising.

    `check=False` throughout: a non-zero exit is data here, not an exception.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return GitFailed("git is not installed on this machine.")
    except subprocess.TimeoutExpired:
        return GitFailed(f"git {args[0]} timed out after {timeout}s.")

    if completed.returncode != 0:
        return GitFailed(
            f"git {args[0]} failed (exit {completed.returncode}).",
            stderr=completed.stderr.strip(),
        )
    return GitOk(completed.stdout.strip())


def slugify(text: str, limit: int = 40) -> str:
    """A branch-safe fragment of a task title.

    Trailing hyphens are stripped after truncation, so a title cut mid-word
    does not produce `task-12-add-support-for-` as a branch name.
    """
    slug = _SLUG_STRIP.sub("-", text.strip().lower()).strip("-")
    return slug[:limit].strip("-") or "task"


def branch_name(task_id: int, title: str) -> str:
    """The branch a task's work happens on.

    Namespaced under `workbench/` so these are obvious in `git branch` and easy
    to clean up in bulk.
    """
    return f"workbench/task-{task_id}-{slugify(title)}"


def clone_path_for(owner: str, repo: str) -> Path:
    """Where this project's clone belongs on this machine.

    Deterministic from the owner and repository name, which is why nothing
    stores it. A stored path would be an absolute path to one machine's disk,
    and this database gets copied between instances — staging restores a
    snapshot of production every deploy, so a stored path would arrive in
    staging still pointing at production's checkout.
    """
    return repos_dir() / f"{owner}-{repo}"


def local_checkout(owner: str, repo: str) -> Path | None:
    """The project's clone, or None if it has not been cloned here.

    Asked of the filesystem rather than the database on purpose. It is one
    stat, it is always right for the instance asking, and it cannot go stale —
    a directory deleted by hand stops being a checkout immediately rather than
    when someone remembers to clear a column.
    """
    path = clone_path_for(owner, repo)
    return path if (path / ".git").exists() else None


def worktree_path_for(task_id: int, title: str) -> Path:
    return worktrees_dir() / f"task-{task_id}-{slugify(title)}"


@dataclass(frozen=True)
class Cloned:
    path: Path


type CloneResult = Cloned | GitFailed


def clone_project(clone_url: str, owner: str, repo: str) -> CloneResult:
    """Clone a project repository, or adopt the clone that is already there.

    Idempotent: an existing checkout is fetched rather than re-cloned, so this
    is safe to call from a button someone may press twice.
    """
    target = clone_path_for(owner, repo)
    if (target / ".git").exists():
        logger.info("Repository already cloned at %s; fetching.", target)
        fetched = _run_git(
            ["fetch", "--all", "--prune"], cwd=target, timeout=NETWORK_TIMEOUT_SECONDS
        )
        if isinstance(fetched, GitFailed):
            return fetched
        return Cloned(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git(
        ["clone", clone_url, str(target)],
        timeout=NETWORK_TIMEOUT_SECONDS,
    )
    if isinstance(result, GitFailed):
        return result
    return Cloned(target)


@dataclass(frozen=True)
class WorktreeReady:
    path: Path
    branch: str


type WorktreeResult = WorktreeReady | GitFailed


def ensure_worktree(repo: Path, task_id: int, title: str, base_branch: str) -> WorktreeResult:
    """Create the task's branch and worktree, or return the existing one.

    Branches from `origin/<base>` when that ref exists so the work starts from
    what GitHub has rather than from whatever this clone happens to be sitting
    on, and falls back to the local branch for a repository with no remote.
    """
    branch = branch_name(task_id, title)
    path = worktree_path_for(task_id, title)

    if path.exists():
        if (path / ".git").exists():
            return WorktreeReady(path, branch)
        return GitFailed(f"{path} exists but is not a worktree. Remove it and retry.")

    path.parent.mkdir(parents=True, exist_ok=True)

    start_point = f"origin/{base_branch}"
    if isinstance(_run_git(["rev-parse", "--verify", start_point], cwd=repo), GitFailed):
        start_point = base_branch

    existing_branch = _run_git(["rev-parse", "--verify", branch], cwd=repo)
    if isinstance(existing_branch, GitOk):
        # The branch survived a previous run whose worktree was removed.
        # Re-attach rather than failing on "branch already exists".
        result = _run_git(["worktree", "add", str(path), branch], cwd=repo)
    else:
        result = _run_git(["worktree", "add", "-b", branch, str(path), start_point], cwd=repo)

    if isinstance(result, GitFailed):
        return result
    return WorktreeReady(path, branch)


def remove_worktree(repo: Path, path: Path) -> GitResult:
    """Detach a worktree and delete its directory.

    `--force` because an abandoned agent run routinely leaves the tree dirty,
    and refusing to clean up would strand the directory permanently. The task
    row is being deleted; its scratch space goes with it.
    """
    result = _run_git(["worktree", "remove", "--force", str(path)], cwd=repo)
    if isinstance(result, GitFailed) and path.exists():
        # Worktree metadata can be missing or stale — the recorded repo may
        # itself be gone. The directory should still not outlive the task.
        shutil.rmtree(path, ignore_errors=True)
        _run_git(["worktree", "prune"], cwd=repo)
        return GitOk("")
    return result


def has_commits(worktree: Path, base_branch: str) -> bool:
    """Whether anything was actually committed on this branch."""
    result = _run_git(["rev-list", "--count", f"{base_branch}..HEAD"], cwd=worktree)
    if isinstance(result, GitFailed):
        return False
    return result.stdout.strip() not in ("", "0")


def diffstat(worktree: Path, base_branch: str) -> str:
    """A summary of what changed, relative to the base branch.

    `--stat` rather than a full diff: this is stored on the run and rendered on
    a phone, and it is bounded by the number of files rather than by the size
    of the change.
    """
    result = _run_git(["diff", "--stat", f"{base_branch}...HEAD"], cwd=worktree)
    if isinstance(result, GitFailed):
        return ""
    return result.stdout


def uncommitted_diffstat(worktree: Path) -> str:
    """Changes the agent left behind without committing.

    An interrupted run still has something worth reporting, which is the reason
    the summary is fed both committed and uncommitted work.
    """
    result = _run_git(["diff", "--stat", "HEAD"], cwd=worktree)
    if isinstance(result, GitFailed):
        return ""
    return result.stdout


def current_commit(worktree: Path) -> str | None:
    result = _run_git(["rev-parse", "HEAD"], cwd=worktree)
    return result.stdout if isinstance(result, GitOk) else None


def run_setup_command(worktree: Path, command: str) -> GitResult:
    """Run the project's setup command inside a fresh worktree.

    A worktree contains tracked files only — no .env, no node_modules, no venv
    — so without this most first builds fail for reasons unrelated to the task.
    Shell-quoted by the user, so it runs through a shell deliberately.
    """
    try:
        completed = subprocess.run(
            command,
            cwd=worktree,
            shell=True,
            capture_output=True,
            text=True,
            timeout=NETWORK_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return GitFailed(f"Setup command timed out after {NETWORK_TIMEOUT_SECONDS}s.")

    if completed.returncode != 0:
        return GitFailed(
            f"Setup command failed (exit {completed.returncode}).",
            stderr=completed.stderr.strip()[-2000:],
        )
    return GitOk(completed.stdout.strip())
