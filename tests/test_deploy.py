"""The automatic deployer's refusals.

The happy path needs a real remote and root, so what is tested here is
everything the deployer must decline to do. Those are the cases that matter:
a deploy that wrongly proceeds discards someone's work or restarts the service
into a schema it does not match, and both are silent until much later.
"""

import subprocess

import pytest

from workbench.deploy import AlreadyCurrent, Deployed, DeployFailed, deploy


def git(path, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A checkout with an `origin` it can actually fetch from.

    origin is a second local repository rather than a mock, so `git fetch` and
    `git merge --ff-only` behave exactly as they will on the server.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "test@example.com")
    git(origin, "config", "user.name", "Test")
    (origin / "README.md").write_text("one\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "first")

    work = tmp_path / "workbench"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")

    # repo_root() walks up for pyproject.toml, so the clone needs one to be
    # recognised as the checkout under management.
    (work / "pyproject.toml").write_text('[project]\nname = "workbench"\n')
    monkeypatch.setattr("workbench.deploy.repo_root", lambda: work)
    monkeypatch.setattr("workbench.config.repo_root", lambda: work)
    return work, origin


def advance_origin(origin) -> None:
    (origin / "README.md").write_text("two\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "second")


def test_no_new_commits_is_a_no_op(checkout, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")

    assert isinstance(deploy(), AlreadyCurrent)


def test_a_checkout_on_another_branch_is_left_alone(checkout, monkeypatch):
    """Someone is working on the server by hand; do not yank the tree."""
    work, origin = checkout
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")
    git(work, "checkout", "-q", "-b", "experiment")
    advance_origin(origin)

    result = deploy()

    assert isinstance(result, DeployFailed)
    assert "leaving it alone" in result.message
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "experiment"


def test_uncommitted_work_blocks_the_fast_forward(checkout, monkeypatch):
    """A dirty checkout is someone's work in progress, not something to discard."""
    work, origin = checkout
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")
    (work / "README.md").write_text("edited by hand\n")
    advance_origin(origin)

    result = deploy()

    assert isinstance(result, DeployFailed)
    assert result.step == "fast-forwarding the checkout"
    assert (work / "README.md").read_text() == "edited by hand\n"


def test_a_diverged_checkout_is_never_rewritten(checkout, monkeypatch):
    """--ff-only, so a local commit is preserved rather than reset away."""
    work, origin = checkout
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")
    (work / "local.txt").write_text("local only\n")
    git(work, "add", ".")
    git(work, "commit", "-qm", "local work")
    local_head = git(work, "rev-parse", "HEAD")
    advance_origin(origin)

    result = deploy()

    assert isinstance(result, DeployFailed)
    assert git(work, "rev-parse", "HEAD") == local_head


def test_a_missing_remote_fails_without_touching_the_checkout(checkout, monkeypatch):
    work, _ = checkout
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")
    head = git(work, "rev-parse", "HEAD")
    git(work, "remote", "set-url", "origin", str(work.parent / "does-not-exist"))

    result = deploy()

    assert isinstance(result, DeployFailed)
    assert result.step == "fetching from origin"
    assert git(work, "rev-parse", "HEAD") == head


def test_deploy_branch_is_configurable(checkout, monkeypatch):
    """A checkout following `release` must ignore commits landing on main."""
    _, origin = checkout
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "release")
    advance_origin(origin)

    result = deploy()

    assert isinstance(result, DeployFailed)
    assert "not 'release'" in result.message


def test_results_are_distinguishable_without_catching_anything():
    """The contract the systemd unit depends on: outcomes are values."""
    assert not isinstance(AlreadyCurrent("abc123"), DeployFailed)
    assert not isinstance(Deployed("abc123"), DeployFailed)
    assert DeployFailed("step", "why").step == "step"
