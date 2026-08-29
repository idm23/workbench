"""Branch naming and the git commands built on it.

Branch names are derived from user-typed task titles, which arrive with
punctuation, emoji, and arbitrary length. git rejects a good deal of that, and
the failure would surface as a run that dies immediately with a git error
rather than as anything a person could act on.
"""

import subprocess

import pytest

from workbench.git.worktrees import (
    GitFailed,
    GitOk,
    WorktreeReady,
    branch_name,
    clone_path_for,
    diffstat,
    ensure_worktree,
    fetch_checkout,
    has_commits,
    local_checkout,
    slugify,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Add route tests", "add-route-tests"),
        ("Fix the /users/{id} route", "fix-the-users-id-route"),
        ("  padded  ", "padded"),
        ("Trailing punctuation!!!", "trailing-punctuation"),
        ("multiple   spaces", "multiple-spaces"),
        ("MiXeD CaSe", "mixed-case"),
    ],
)
def test_slugify(title, expected):
    assert slugify(title) == expected


def test_slugify_falls_back_when_nothing_survives():
    """A title of pure punctuation still has to produce a usable branch."""
    assert slugify("!!!") == "task"
    assert slugify("") == "task"


def test_slugify_does_not_end_in_a_hyphen_after_truncation():
    """git accepts it, but `task-12-add-support-for-` reads like a bug."""
    slug = slugify("add support for a really long thing that keeps going", limit=20)

    assert not slug.endswith("-")
    assert len(slug) <= 20


def test_branch_name_is_namespaced_and_includes_the_id():
    assert branch_name(12, "Add route tests") == "workbench/task-12-add-route-tests"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repository with one commit, and worktrees pointed at tmp."""
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "workbench.db"))

    path = tmp_path / "repo"
    path.mkdir()
    run = lambda *args: subprocess.run(args, cwd=path, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-b", "main")
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    run("git", "add", ".")
    run("git", "commit", "-m", "first")
    return path


def test_ensure_worktree_creates_a_branch_and_checkout(repo):
    result = ensure_worktree(repo, task_id=1, title="Add route tests", base_branch="main")

    assert isinstance(result, WorktreeReady)
    assert result.branch == "workbench/task-1-add-route-tests"
    assert (result.path / "README.md").exists()


def test_ensure_worktree_is_idempotent(repo):
    first = ensure_worktree(repo, task_id=1, title="Same task", base_branch="main")
    second = ensure_worktree(repo, task_id=1, title="Same task", base_branch="main")

    assert first == second


def test_has_commits_is_false_before_any_work(repo):
    result = ensure_worktree(repo, task_id=1, title="Nothing yet", base_branch="main")

    assert isinstance(result, WorktreeReady)
    assert has_commits(result.path, "main") is False


def test_has_commits_and_diffstat_see_committed_work(repo):
    result = ensure_worktree(repo, task_id=1, title="Do work", base_branch="main")
    assert isinstance(result, WorktreeReady)
    worktree = result.path

    (worktree / "new.txt").write_text("content\n")
    for args in (
        ("git", "add", "."),
        ("git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", "work"),
    ):
        subprocess.run(args, cwd=worktree, check=True, capture_output=True)

    assert has_commits(worktree, "main") is True
    assert "new.txt" in diffstat(worktree, "main")


def test_diffstat_of_a_missing_worktree_is_empty_not_an_error(tmp_path):
    """Callers render this straight into a page; it must never raise."""
    assert diffstat(tmp_path / "gone", "main") == ""


def test_git_failure_is_returned_not_raised(repo):
    """The result-type contract: a bad ref is data, not an exception."""
    result = ensure_worktree(repo, task_id=1, title="x", base_branch="no-such-branch")

    assert isinstance(result, GitFailed)
    assert result.stderr  # git's own explanation, for the page


def test_worktree_survives_its_directory_being_deleted(repo):
    """The branch outlives the checkout, so a wiped worktree can re-attach.

    `data/` is disposable in a way the branch is not — losing the directory
    must not strand the work that was committed to it.
    """
    first = ensure_worktree(repo, task_id=7, title="Recoverable", base_branch="main")
    assert isinstance(first, WorktreeReady)

    subprocess.run(
        ["git", "worktree", "remove", "--force", str(first.path)],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    second = ensure_worktree(repo, task_id=7, title="Recoverable", base_branch="main")

    assert isinstance(second, WorktreeReady)
    assert second.branch == first.branch


# --- Fetching before branching ----------------------------------------------
#
# `ensure_worktree` branches from `origin/<base>`, which is only as fresh as
# the checkout's last fetch. A clone made once and never fetched again is
# exactly the trap this fixes: a task started days after the clone would
# otherwise branch from whatever `origin/main` happened to be back then.


@pytest.fixture
def cloned_repo(tmp_path, monkeypatch):
    """A checkout with an `origin` it can actually fetch from.

    A second local repository rather than a mock, so `git fetch` and
    `origin/main` behave exactly as they would against GitHub.
    """
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "workbench.db"))

    origin = tmp_path / "origin"
    origin.mkdir()

    def git(*args: str, cwd=origin) -> None:
        subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (origin / "README.md").write_text("hello\n")
    git("add", ".")
    git("commit", "-m", "first")

    checkout = tmp_path / "checkout"
    git("clone", str(origin), str(checkout), cwd=tmp_path)
    return origin, checkout


def _commit_more(origin, name: str) -> None:
    (origin / name).write_text("later\n")
    subprocess.run(("git", "add", "."), cwd=origin, check=True, capture_output=True)
    subprocess.run(
        ("git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-m", name),
        cwd=origin,
        check=True,
        capture_output=True,
    )


def test_a_worktree_branches_from_a_stale_origin_without_a_fetch(cloned_repo):
    """Pinning the trap itself, before pinning its fix."""
    origin, checkout = cloned_repo
    _commit_more(origin, "new.txt")

    result = ensure_worktree(checkout, task_id=1, title="x", base_branch="main")

    assert isinstance(result, WorktreeReady)
    assert not (result.path / "new.txt").exists()


def test_fetching_first_picks_up_what_origin_has_now(cloned_repo):
    origin, checkout = cloned_repo
    _commit_more(origin, "new.txt")

    assert isinstance(fetch_checkout(checkout), GitOk)
    result = ensure_worktree(checkout, task_id=1, title="x", base_branch="main")

    assert isinstance(result, WorktreeReady)
    assert (result.path / "new.txt").exists()


def test_fetch_checkout_is_a_no_op_without_a_remote(repo):
    """`clone_project` calls this even for a from-scratch repository in tests."""
    assert isinstance(fetch_checkout(repo), GitOk)


# --- Deriving the checkout ---------------------------------------------------
#
# The clone's location used to be stored on the project row. It is derived now
# because this database is copied between instances: staging restores a
# snapshot of production on every deploy, so a stored absolute path arrived in
# staging still pointing at production's checkout — and once runs exist, that
# means staging creating worktrees inside production's repository.


def test_an_uncloned_project_has_no_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "workbench.db"))

    assert local_checkout("idm23", "workbench") is None


def test_a_directory_without_git_is_not_a_checkout(tmp_path, monkeypatch):
    """A half-finished clone, or someone's mkdir. Not something to run in."""
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "workbench.db"))
    clone_path_for("idm23", "workbench").mkdir(parents=True)

    assert local_checkout("idm23", "workbench") is None


def test_a_real_clone_is_found(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "workbench.db"))
    path = clone_path_for("idm23", "workbench")
    (path / ".git").mkdir(parents=True)

    assert local_checkout("idm23", "workbench") == path


def test_each_instance_resolves_its_own_checkout(tmp_path, monkeypatch):
    """The whole reason this is derived rather than stored.

    Two installs share a machine and a database, and must never resolve to each
    other's clone.
    """
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "production" / "workbench.db"))
    production = clone_path_for("idm23", "workbench")
    (production / ".git").mkdir(parents=True)
    assert local_checkout("idm23", "workbench") == production

    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "staging" / "workbench.db"))
    assert clone_path_for("idm23", "workbench") != production
    # Staging has restored production's database but has no clone of its own,
    # so it correctly reports having nothing to work in.
    assert local_checkout("idm23", "workbench") is None
