"""Reading the checkout's revision.

Small, but it feeds a health check and the staging acceptance gate, so its
failure modes matter more than its size suggests: an exception here would take
down `/healthz`, and a wrong answer would let a deploy that never restarted the
service report success.
"""

import subprocess

import pytest

from workbench.git.revision import UNKNOWN, head_revision


def git(path, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A real repository with one commit."""
    work = tmp_path / "repo"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("one\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "first")
    monkeypatch.setattr("workbench.git.revision.repo_root", lambda: work)
    return work


def test_reports_the_current_commit(checkout):
    assert head_revision(short=False) == git(checkout, "rev-parse", "HEAD")


def test_short_is_the_default_and_is_a_prefix(checkout):
    full = git(checkout, "rev-parse", "HEAD")

    assert head_revision() == git(checkout, "rev-parse", "--short", "HEAD")
    assert full.startswith(head_revision())


def test_it_follows_the_checkout(checkout):
    """Uncached on purpose — the deployer reads it either side of a merge."""
    before = head_revision()
    (checkout / "README.md").write_text("two\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "second")

    assert head_revision() != before
    assert head_revision() == git(checkout, "rev-parse", "--short", "HEAD")


def test_a_directory_that_is_not_a_repository_is_unknown(tmp_path, monkeypatch):
    """A tarball rather than a clone. Not worth failing a health check over."""
    monkeypatch.setattr("workbench.git.revision.repo_root", lambda: tmp_path)

    assert head_revision() == UNKNOWN


def test_a_repository_with_no_commits_is_unknown(tmp_path, monkeypatch):
    """`git rev-parse HEAD` fails before the first commit exists."""
    git(tmp_path, "init", "-q", "-b", "main")
    monkeypatch.setattr("workbench.git.revision.repo_root", lambda: tmp_path)

    assert head_revision() == UNKNOWN


def test_a_missing_directory_does_not_raise(tmp_path, monkeypatch):
    """Every caller is answering a health check or writing a log line."""
    monkeypatch.setattr("workbench.git.revision.repo_root", lambda: tmp_path / "gone")

    assert head_revision() == UNKNOWN
