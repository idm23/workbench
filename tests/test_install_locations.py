"""Deciding where a deployment lives, and moving one there safely.

The install now has an opinion about its own location: a checkout in somebody's
home cannot be served by a separate account, because Ubuntu creates home
directories mode 0750 and the service cannot traverse one. So the first install
copies the checkout to /srv and hands off to it.

What is pinned here is mostly what must *not* happen. The copy leaves the
original alone, so a relocation that goes wrong costs nothing. It does not
carry a virtualenv whose scripts name the old path. It does not copy a live
database as a file. And it does not silently overwrite a deployment that is
already there, because re-running the abandoned checkout's installer is a
thing people will do.
"""

import os
import pwd
import sqlite3

import pytest

from workbench import install
from workbench.config import deployment_root


@pytest.fixture
def account():
    """Whoever is running the tests, standing in for the service account."""
    return pwd.getpwuid(os.getuid())


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A plausible checkout: a venv, caches, a database, a clone, a worktree."""
    source = tmp_path / "checkout"
    (source / "src" / "workbench").mkdir(parents=True)
    (source / "src" / "workbench" / "app.py").write_text("# the app\n")
    (source / "pyproject.toml").write_text('[project]\nname = "workbench"\n')

    (source / ".venv" / "bin").mkdir(parents=True)
    (source / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (source / "src" / "workbench" / "__pycache__").mkdir()
    (source / "src" / "workbench" / "__pycache__" / "app.pyc").write_bytes(b"\x00")

    (source / "data" / "repos" / "idm23" / "workbench").mkdir(parents=True)
    (source / "data" / "repos" / "idm23" / "workbench" / "README.md").write_text("cloned\n")
    (source / "data" / "worktrees" / "task-18").mkdir(parents=True)
    (source / "data" / "worktrees" / "task-18" / "file.txt").write_text("disposable\n")

    monkeypatch.setattr("workbench.install.repo_root", lambda: source)
    monkeypatch.setattr("workbench.config.repo_root", lambda: source)
    return source


def a_database(path, *, rows=("alpha",)):
    """A database in WAL mode with committed rows, as production is."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER, worktree_path TEXT)")
    for index, _ in enumerate(rows):
        connection.execute("INSERT INTO tasks VALUES (?, NULL)", (index,))
    connection.commit()
    return connection


# --- Whether to move at all ---------------------------------------------------


def test_a_checkout_somewhere_else_needs_relocating(checkout):
    assert install.needs_relocation()


def test_the_deployment_itself_does_not(monkeypatch, checkout):
    monkeypatch.setenv("WORKBENCH_DEPLOYMENT_ROOT", str(checkout))

    assert not install.needs_relocation()
    assert deployment_root() == checkout


# --- What travels, and what does not ------------------------------------------


def test_the_virtualenv_does_not_travel(checkout, tmp_path, monkeypatch, account):
    """A venv bakes absolute paths into its scripts, so a copied one points
    back at the checkout it came from — which is the directory being left."""
    target = _relocate_into(tmp_path / "srv", checkout, monkeypatch, account)

    assert not (target / ".venv").exists()


def test_caches_do_not_travel(checkout, tmp_path, monkeypatch, account):
    target = _relocate_into(tmp_path / "srv", checkout, monkeypatch, account)

    assert not (target / "src" / "workbench" / "__pycache__").exists()


def test_the_source_and_the_clones_do_travel(checkout, tmp_path, monkeypatch, account):
    """Clones are the expensive thing on the disk; re-cloning every project
    would make a relocation cost a great deal more than it needs to."""
    target = _relocate_into(tmp_path / "srv", checkout, monkeypatch, account)

    assert (target / "src" / "workbench" / "app.py").read_text() == "# the app\n"
    assert (target / "data" / "repos" / "idm23" / "workbench" / "README.md").exists()


def test_worktrees_do_not_travel(checkout, tmp_path, monkeypatch, account):
    """Disposable by design, and their git metadata names the old path — so
    copying them would carry a broken reference into the new tree."""
    target = _relocate_into(tmp_path / "srv", checkout, monkeypatch, account)

    assert not (target / "data" / "worktrees" / "task-18").exists()


def test_the_original_checkout_is_left_exactly_as_it_was(checkout, tmp_path, monkeypatch, account):
    """Copy, not move. This is what makes the relocation need no confirmation
    and no backup step: rolling back is pointing the old checkout at itself."""
    a_database(checkout / "data" / "workbench.db").close()

    _relocate_into(tmp_path / "srv", checkout, monkeypatch, account)

    assert (checkout / ".venv" / "bin" / "python").exists()
    assert (checkout / "data" / "workbench.db").exists()
    assert (checkout / "data" / "worktrees" / "task-18" / "file.txt").exists()


# --- The database -------------------------------------------------------------


def test_a_database_is_copied_through_the_backup_api(tmp_path):
    """Committed rows live in the write-ahead log until a checkpoint, so a file
    copy of a live database is either stale or torn. This is the assertion that
    would fail if someone ever replaced this with shutil.copy.
    """
    source = tmp_path / "live.db"
    connection = a_database(source, rows=("alpha", "beta"))  # deliberately left open, in WAL mode

    install.copy_database(source, tmp_path / "copy.db")

    copied = sqlite3.connect(tmp_path / "copy.db")
    assert copied.execute("SELECT count(*) FROM tasks").fetchone()[0] == 2
    copied.close()
    connection.close()


def test_the_database_travels_with_its_rows(checkout, tmp_path, monkeypatch, account):
    a_database(checkout / "data" / "workbench.db", rows=("alpha", "beta", "gamma")).close()

    target = _relocate_into(tmp_path / "srv", checkout, monkeypatch, account)

    copied = sqlite3.connect(target / "data" / "workbench.db")
    assert copied.execute("SELECT count(*) FROM tasks").fetchone()[0] == 3
    copied.close()


def test_the_write_ahead_log_is_not_copied_as_a_file(checkout, tmp_path, monkeypatch, account):
    connection = a_database(checkout / "data" / "workbench.db")

    target = _relocate_into(tmp_path / "srv", checkout, monkeypatch, account)
    connection.close()

    assert not (target / "data" / "workbench.db-wal").exists()
    assert not (target / "data" / "workbench.db-shm").exists()


def test_worktree_paths_pointing_at_the_old_checkout_are_cleared(
    checkout, tmp_path, monkeypatch, account
):
    """The rows would otherwise name directories outside the deployment that
    the service account cannot write. Clearing the path means the next run
    makes a fresh worktree; the branch is untouched, so no work is lost."""
    connection = a_database(checkout / "data" / "workbench.db")
    connection.execute("UPDATE tasks SET worktree_path = ?", (f"{checkout}/data/worktrees/t18",))
    connection.commit()
    connection.close()

    target = _relocate_into(tmp_path / "srv", checkout, monkeypatch, account)

    copied = sqlite3.connect(target / "data" / "workbench.db")
    assert copied.execute("SELECT worktree_path FROM tasks").fetchone()[0] is None
    copied.close()


def test_a_worktree_path_somewhere_else_is_left_alone(checkout, tmp_path, monkeypatch, account):
    """Only paths into the checkout being abandoned are stale."""
    connection = a_database(checkout / "data" / "workbench.db")
    connection.execute("UPDATE tasks SET worktree_path = ?", ("/srv/elsewhere/wt",))
    connection.commit()
    connection.close()

    target = _relocate_into(tmp_path / "srv", checkout, monkeypatch, account)

    copied = sqlite3.connect(target / "data" / "workbench.db")
    assert copied.execute("SELECT worktree_path FROM tasks").fetchone()[0] == "/srv/elsewhere/wt"
    copied.close()


# --- Not clobbering a deployment that is already there ------------------------


def test_an_existing_deployment_is_handed_off_to_rather_than_overwritten(
    checkout, tmp_path, monkeypatch, account
):
    """Re-running the abandoned checkout's install.sh is a thing people do, and
    it must not copy a months-old tree over the live one."""
    existing = tmp_path / "srv"
    existing.mkdir(parents=True)
    # `pyproject.toml` is the marker, because it is what `repo_root()` keys on.
    # Keying on `.git` looked equivalent and was not: a tree delivered by
    # anything but a clone has none, and the guard would silently never fire.
    (existing / "pyproject.toml").write_text('[project]\nname = "workbench"\n')
    (existing / "src").mkdir()
    (existing / "src" / "sentinel.txt").write_text("the real deployment\n")
    a_database(existing / "data" / "workbench.db", rows=("live", "rows")).close()

    target = _relocate_into(existing, checkout, monkeypatch, account)

    assert target == existing
    assert (existing / "src" / "sentinel.txt").exists()
    assert not (existing / "src" / "workbench" / "app.py").exists()


def _relocate_into(target, checkout, monkeypatch, account):
    """Run a relocation without needing root to chown or systemd to stop."""
    monkeypatch.setenv("WORKBENCH_DEPLOYMENT_ROOT", str(target))
    monkeypatch.setattr(install, "_chown_tree", lambda *_args: None)
    monkeypatch.setattr(install, "systemd_is_running", lambda: False)
    return install.relocate(account)
