"""The automatic deployer.

Split in two because the halves are testable to different depths.
`advance_checkout` decides whether to deploy and moves the checkout, and every
refusal lives there — wrong branch, dirty tree, diverged history. Those are the
cases that matter most: a deploy that wrongly proceeds discards someone's work
or restarts the service into a schema it does not match, and both are silent
until much later. All of it runs against a real git remote here.

`rebuild_and_restart` needs root, a virtualenv, and systemd, so it is exercised
on the server rather than in this file.
"""

import importlib.util
import sqlite3
import subprocess
from pathlib import Path

import pytest

from workbench.deploy import (
    ACCEPTANCE_EXIT_NOT_REPORTED,
    Advanced,
    AlreadyCurrent,
    Deployed,
    DeployFailed,
    advance_checkout,
    deploy,
    restore_snapshot,
    run_acceptance,
)


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


# --- Actually advancing ------------------------------------------------------
#
# The refusals above are only half the contract. These check the deployer does
# the thing it exists for, which nothing else in this file proves.


def test_new_commits_are_fast_forwarded_in(checkout, monkeypatch):
    work, origin = checkout
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")
    advance_origin(origin)

    result = advance_checkout()

    assert isinstance(result, Advanced)
    assert (work / "README.md").read_text() == "two\n"
    assert git(work, "rev-parse", "HEAD") == git(origin, "rev-parse", "HEAD")


def test_advancing_reports_the_revision_it_landed_on(checkout, monkeypatch):
    work, origin = checkout
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")
    advance_origin(origin)

    result = advance_checkout()

    assert isinstance(result, Advanced)
    assert result.revision == git(work, "rev-parse", "--short", "HEAD")


def test_several_commits_land_in_one_go(checkout, monkeypatch):
    """A weekend of merges is one deploy, not one per commit."""
    work, origin = checkout
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")
    for index in range(3):
        (origin / f"file{index}.txt").write_text("x\n")
        git(origin, "add", ".")
        git(origin, "commit", "-qm", f"change {index}")

    assert isinstance(advance_checkout(), Advanced)
    assert git(work, "rev-parse", "HEAD") == git(origin, "rev-parse", "HEAD")
    assert (work / "file2.txt").exists()


def test_advancing_twice_is_a_no_op_the_second_time(checkout, monkeypatch):
    """The timer fires every few minutes; all but one tick must do nothing."""
    _, origin = checkout
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")
    advance_origin(origin)

    assert isinstance(advance_checkout(), Advanced)
    assert isinstance(advance_checkout(), AlreadyCurrent)


def test_a_failed_rebuild_is_reported_not_raised(checkout, monkeypatch):
    """deploy() composes the halves: a rebuild failure surfaces as a result."""
    _, origin = checkout
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")
    advance_origin(origin)
    monkeypatch.setattr(
        "workbench.deploy.rebuild_and_restart",
        lambda: DeployFailed("applying migrations", "boom"),
    )

    result = deploy()

    assert isinstance(result, DeployFailed)
    assert result.step == "applying migrations"


def test_a_successful_rebuild_reports_the_new_revision(checkout, monkeypatch):
    work, origin = checkout
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")
    advance_origin(origin)
    monkeypatch.setattr("workbench.deploy.rebuild_and_restart", lambda: None)

    result = deploy()

    assert isinstance(result, Deployed)
    assert result.revision == git(work, "rev-parse", "--short", "HEAD")


def test_nothing_is_rebuilt_when_there_is_nothing_to_deploy(checkout, monkeypatch):
    """The common tick must not pay for a sync and a restart."""
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "main")
    calls: list[int] = []
    monkeypatch.setattr(
        "workbench.deploy.rebuild_and_restart",
        lambda: calls.append(1),  # pyright: ignore[reportArgumentType]
    )

    assert isinstance(deploy(), AlreadyCurrent)
    assert calls == []


# --- Restoring a snapshot before migrating -----------------------------------
#
# Staging copies production's database over its own before running migrations,
# so a revision meets real rows before it meets the machine you depend on.
# Production must never do this: it would be overwriting live data every deploy.


def make_database(path, rows: int = 3) -> None:
    """A WAL-mode database with content, like the one on the server."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        connection.executemany(
            "INSERT INTO users (name) VALUES (?)", [(f"user-{n}",) for n in range(rows)]
        )


def count_users(path) -> int:
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT count(*) FROM users").fetchone()[0]


def test_no_restore_configured_is_a_no_op(checkout, monkeypatch):
    monkeypatch.delenv("WORKBENCH_RESTORE_FROM", raising=False)

    assert restore_snapshot() is None


def test_a_snapshot_replaces_this_instances_database(checkout, tmp_path, monkeypatch):
    work, _ = checkout
    source = tmp_path / "prod" / "workbench.db"
    make_database(source, rows=5)
    monkeypatch.setenv("WORKBENCH_RESTORE_FROM", str(source))
    monkeypatch.setenv("WORKBENCH_DB", str(work / "data" / "workbench.db"))

    assert restore_snapshot() is None
    assert count_users(work / "data" / "workbench.db") == 5


def test_a_snapshot_overwrites_what_staging_already_had(checkout, tmp_path, monkeypatch):
    """Staging is disposable; each deploy starts from production's rows again."""
    work, _ = checkout
    source = tmp_path / "prod" / "workbench.db"
    target = work / "data" / "workbench.db"
    make_database(source, rows=5)
    make_database(target, rows=99)
    monkeypatch.setenv("WORKBENCH_RESTORE_FROM", str(source))
    monkeypatch.setenv("WORKBENCH_DB", str(target))

    assert restore_snapshot() is None
    assert count_users(target) == 5


def test_restoring_from_a_live_wal_database_captures_committed_rows(
    checkout, tmp_path, monkeypatch
):
    """The reason this uses sqlite3's backup API rather than copying the file.

    With WAL, committed data lives partly in the write-ahead log, so a plain
    copy of the .db can miss rows that are definitely committed.
    """
    work, _ = checkout
    source = tmp_path / "prod" / "workbench.db"
    make_database(source, rows=2)

    held_open = sqlite3.connect(source)
    held_open.execute("INSERT INTO users (name) VALUES ('written-into-the-wal')")
    held_open.commit()

    monkeypatch.setenv("WORKBENCH_RESTORE_FROM", str(source))
    monkeypatch.setenv("WORKBENCH_DB", str(work / "data" / "workbench.db"))
    try:
        assert restore_snapshot() is None
        assert count_users(work / "data" / "workbench.db") == 3
    finally:
        held_open.close()


def test_a_missing_snapshot_is_reported_not_ignored(checkout, tmp_path, monkeypatch):
    """Silently skipping would make staging quietly stop testing what it exists for."""
    monkeypatch.setenv("WORKBENCH_RESTORE_FROM", str(tmp_path / "nope.db"))

    result = restore_snapshot()

    assert isinstance(result, DeployFailed)
    assert result.step == "restoring the database snapshot"


def test_acceptance_only_runs_where_data_is_disposable(checkout, monkeypatch):
    """Acceptance creates records, so it must not touch data anyone relies on.

    Gated on restoring a snapshot rather than on "is not production": any
    second install — a CI instance, a scratch one — would otherwise have its
    rows quietly rewritten by a deploy.
    """
    monkeypatch.delenv("WORKBENCH_RESTORE_FROM", raising=False)
    monkeypatch.setenv("WORKBENCH_INSTANCE", "citest")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "workbench.deploy._run",
        lambda argv, **_: calls.append(argv),  # pyright: ignore[reportArgumentType]
    )

    run_acceptance()

    assert calls == []


def test_acceptance_runs_when_a_snapshot_is_restored(checkout, tmp_path, monkeypatch):
    work, _ = checkout
    source = tmp_path / "prod" / "workbench.db"
    make_database(source)
    monkeypatch.setenv("WORKBENCH_RESTORE_FROM", str(source))
    monkeypatch.setenv("WORKBENCH_INSTANCE", "staging")
    (work / "scripts").mkdir(parents=True, exist_ok=True)
    (work / "scripts" / "staging_acceptance.py").write_text("")

    calls: list[list[str]] = []

    def fake_run(argv, **_):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("workbench.deploy._run", fake_run)

    run_acceptance()

    assert any("staging_acceptance.py" in " ".join(argv) for argv in calls)


def test_the_not_reported_exit_code_agrees_across_the_two_files():
    """deploy.py reads an exit code that staging_acceptance.py defines.

    Nothing imports one from the other — the script is not a package — so this
    is the only thing stopping the two drifting apart and turning "the token
    expired" back into a generic failure message.
    """
    spec = importlib.util.spec_from_file_location(
        "staging_acceptance",
        Path(__file__).resolve().parent.parent / "scripts" / "staging_acceptance.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.EXIT_NOT_REPORTED == ACCEPTANCE_EXIT_NOT_REPORTED


def test_production_never_runs_acceptance(checkout, monkeypatch):
    monkeypatch.delenv("WORKBENCH_RESTORE_FROM", raising=False)
    monkeypatch.delenv("WORKBENCH_INSTANCE", raising=False)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "workbench.deploy._run",
        lambda argv, **_: calls.append(argv),  # pyright: ignore[reportArgumentType]
    )

    run_acceptance()

    assert calls == []


def test_an_instance_refuses_to_restore_over_itself(checkout, tmp_path, monkeypatch):
    """Misconfiguring production this way would wipe it on every deploy."""
    source = tmp_path / "prod" / "workbench.db"
    make_database(source)
    monkeypatch.setenv("WORKBENCH_RESTORE_FROM", str(source))
    monkeypatch.setenv("WORKBENCH_DB", str(source))

    result = restore_snapshot()

    assert isinstance(result, DeployFailed)
    assert "own database" in result.message
    assert count_users(source) == 3
