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
    WorktreeReady,
    branch_name,
    diffstat,
    ensure_worktree,
    has_commits,
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
