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
from workbench.git.github import InvalidReference, parse_repo_reference

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


def fetch_checkout(repo: Path) -> GitResult:
    """Bring a clone's remote-tracking branches up to date.

    `ensure_worktree` branches from `origin/<base>` when that ref exists, but
    that ref is only as fresh as the last fetch. A clone's first fetch happens
    here or in `clone_project`; nothing after that did, which meant a task
    started days after the initial clone silently branched from whatever
    `origin/main` happened to be at clone time — a repository that is present
    on disk is not the same thing as one whose remote-tracking branches are
    current. Safe to call on a repository with no remote at all: git treats
    `fetch --all` with nothing configured as a no-op rather than an error.
    """
    return _run_git(["fetch", "--all", "--prune"], cwd=repo, timeout=NETWORK_TIMEOUT_SECONDS)


def clone_project(clone_url: str, owner: str, repo: str) -> CloneResult:
    """Clone a project repository, or adopt the clone that is already there.

    Idempotent: an existing checkout is fetched rather than re-cloned, so this
    is safe to call from a button someone may press twice.
    """
    target = clone_path_for(owner, repo)
    if (target / ".git").exists():
        logger.info("Repository already cloned at %s; fetching.", target)
        fetched = fetch_checkout(target)
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


def _resolve_ref(repo: Path, base_branch: str) -> str:
    """`origin/<base>` when that ref exists, so work starts from what GitHub
    has rather than from whatever this clone happens to be sitting on;
    falls back to the plain name otherwise — a repository with no remote,
    or a `base_branch` that already names a local-only branch (another
    task's own, never pushed).

    Safe to call with a worktree as well as the clone: a worktree shares its
    repository's refs, which is what lets the helpers below resolve a base
    without being handed the clone separately."""
    candidate = f"origin/{base_branch}"
    if isinstance(_run_git(["rev-parse", "--verify", candidate], cwd=repo), GitFailed):
        return base_branch
    return candidate


def ensure_worktree(repo: Path, task_id: int, title: str, base_branch: str) -> WorktreeResult:
    """Create the task's branch and worktree, or return the existing one."""
    branch = branch_name(task_id, title)
    path = worktree_path_for(task_id, title)

    if path.exists():
        if (path / ".git").exists():
            return WorktreeReady(path, branch)
        return GitFailed(f"{path} exists but is not a worktree. Remove it and retry.")

    path.parent.mkdir(parents=True, exist_ok=True)
    start_point = _resolve_ref(repo, base_branch)

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


@dataclass(frozen=True)
class Synced:
    """The branch is now at (or already was at) the tip of its origin."""

    message: str


@dataclass(frozen=True)
class SyncRefused:
    """Safe to leave alone rather than force through.

    Either the working tree has uncommitted changes — someone's in-progress
    work, on a worktree pressing a button should never touch — or the branch
    has commits of its own, which is not a fast-forward and would need an
    actual merge or rebase to resolve.
    """

    message: str


type SyncResult = Synced | SyncRefused | GitFailed


def sync_worktree(repo: Path, worktree: Path, base_branch: str) -> SyncResult:
    """Fast-forward a task's branch onto its origin, or refuse.

    A task's branch is created once and never touched again on its own —
    `ensure_worktree` only looks at `base_branch` the first time a worktree is
    made. Left long enough between planning and approving, it drifts behind
    whatever `base_branch` has since gained, which is a problem for exactly
    the same reason the original stale-clone bug was: an agent working from
    it does not have what has since landed.

    `git merge --ff-only` is the whole safety mechanism here, not a detail:
    it succeeds silently when there is nothing to reconcile and fails loudly
    the moment there is something real to lose, rather than this function
    having to work out the difference itself.
    """
    status = _run_git(["status", "--porcelain"], cwd=worktree)
    if isinstance(status, GitFailed):
        return status
    if status.stdout.strip():
        return SyncRefused(
            "This task's worktree has uncommitted changes — commit or discard them first."
        )

    fetched = fetch_checkout(repo)
    if isinstance(fetched, GitFailed):
        return fetched

    ref = _resolve_ref(repo, base_branch)
    merged = _run_git(["merge", "--ff-only", ref], cwd=worktree)
    if isinstance(merged, GitFailed):
        return SyncRefused(
            f"This task's branch has commits of its own and cannot be fast-forwarded "
            f"to {base_branch}. Merge or rebase it by hand."
        )
    return Synced(merged.stdout)


def has_commits(worktree: Path, base_branch: str) -> bool | GitFailed:
    """Whether anything was actually committed on this branch.

    Returns `GitFailed` rather than False when the question could not be
    asked. The two are not the same and the difference is expensive: "the
    agent committed nothing" is a fine, expected outcome, while "the base ref
    does not resolve" means work exists and nobody was told. Collapsing them
    is how a run that had made a commit reported that it had not.

    The base is resolved the same way `ensure_worktree` resolves it, which is
    the bug that made the distinction matter — see `_resolve_ref`.
    """
    ref = _resolve_ref(worktree, base_branch)
    result = _run_git(["rev-list", "--count", f"{ref}..HEAD"], cwd=worktree)
    if isinstance(result, GitFailed):
        return result
    return result.stdout.strip() not in ("", "0")


def diffstat(worktree: Path, base_branch: str) -> str:
    """A summary of what changed, relative to the base branch.

    `--stat` rather than a full diff: this is stored on the run and rendered on
    a phone, and it is bounded by the number of files rather than by the size
    of the change.

    Resolves the base the same way `ensure_worktree` does. A clone has a local
    branch only for the one it checked out, so a task branched from anything
    else — `staging`, say — has only `origin/staging` to compare against, and
    the bare name silently produced an empty diffstat.
    """
    result = _run_git(
        ["diff", "--stat", f"{_resolve_ref(worktree, base_branch)}...HEAD"], cwd=worktree
    )
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


def ensure_push_remote(worktree: Path) -> GitResult:
    """Point `origin`'s *push* URL at SSH, leaving its fetch URL alone.

    Every clone is made from `projects.github_url`, which `RepoRef.url` always
    builds as HTTPS — so `origin` is HTTPS, and a push asks for a password
    GitHub stopped accepting years ago. Nothing noticed because fetching a
    public repository needs no credentials: push is the first operation in a
    run that authenticates at all, and no run reached it until one did.

    Split rather than switched. Making the fetch URL SSH too would mean a key
    is needed just to *add* a public project, which is a worse trade than the
    one bug it fixes. `remote.origin.pushurl` is git's own answer to exactly
    this, and it matches the split the deployment already has: the deploy key
    pushes, the API token opens pull requests, and reading needs neither.

    Called on the way into every push rather than at clone time, because the
    clones that need it most already exist and nothing re-clones them.
    Idempotent, and a no-op once the URL is already SSH.
    """
    current = _run_git(["remote", "get-url", "--push", "origin"], cwd=worktree)
    if isinstance(current, GitFailed):
        return current

    url = current.stdout.strip()
    if url.startswith("git@"):
        return GitOk(url)

    ref = parse_repo_reference(url)
    if isinstance(ref, InvalidReference):
        # Not GitHub, or a form nothing here understands. Left exactly as it
        # is: guessing at a push URL is how a run pushes somewhere nobody
        # meant it to.
        return GitFailed(f"Cannot push: {url!r} is not a GitHub remote this can authenticate to.")

    return _run_git(["remote", "set-url", "--push", "origin", ref.ssh_url], cwd=worktree)


def push_branch(worktree: Path, branch: str) -> GitResult:
    """Publish a task's branch, so a pull request has something to point at.

    Runs from the worktree rather than the clone: the branch is checked out
    here, and this is the directory whose remote the deploy key is for. A
    worktree shares its repository's config, so repairing the push URL from
    here fixes the clone itself — and therefore fixes it for a person who
    later pushes from a terminal, not only for runs.

    `--set-upstream` so that anyone who later opens a terminal in this
    worktree can `git push` with no arguments and get the same thing. Given
    the network timeout rather than the local one — this is the one git
    operation in a run that talks to GitHub.
    """
    prepared = ensure_push_remote(worktree)
    if isinstance(prepared, GitFailed):
        return prepared

    return _run_git(
        ["push", "--set-upstream", "origin", branch],
        cwd=worktree,
        timeout=NETWORK_TIMEOUT_SECONDS,
    )


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
