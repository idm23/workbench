"""The deploy-cycle test's own scaffolding.

Worth covering because of what it guards: a suite that goes red over its own
temporary files is one people stop believing, and the thing this one guards is
the automatic deploy path. A false negative here is more expensive than the
scaffolding it protects.

The failure being defended against is real and was seen on CI — `shutil.rmtree`
raising ENOTEMPTY on a `.git` directory that was empty when it was scanned,
because git had forked background maintenance that was still writing into it.
"""

import logging
import shutil
import subprocess

import pytest

from scripts.test_deploy_cycle import quiet_git, remove_tree


def a_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return path


def test_it_removes_an_ordinary_tree(tmp_path):
    target = tmp_path / "scratch"
    (target / "nested").mkdir(parents=True)
    (target / "nested" / "file.txt").write_text("x")

    remove_tree(target)

    assert not target.exists()


def test_a_missing_directory_is_not_an_error(tmp_path):
    remove_tree(tmp_path / "never-existed")


def test_it_retries_before_giving_up(tmp_path, monkeypatch):
    """A detached writer usually finishes in well under a second."""
    target = tmp_path / "scratch"
    target.mkdir()
    attempts = []
    real = shutil.rmtree

    def flaky(path, *args, **kwargs):
        attempts.append(path)
        if len(attempts) < 3:
            raise OSError(39, "Directory not empty")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", flaky)

    remove_tree(target)

    assert len(attempts) == 3
    assert not target.exists()


def test_it_never_raises_when_the_directory_will_not_go(tmp_path, monkeypatch, caplog):
    """The whole point: deleting scaffolding must not fail the deploy test."""
    target = tmp_path / "scratch"
    target.mkdir()
    (target / "leftover.txt").write_text("something appeared")

    def always_fails(path, *args, **kwargs):
        raise OSError(39, "Directory not empty")

    monkeypatch.setattr(shutil, "rmtree", always_fails)

    with caplog.at_level(logging.WARNING):
        remove_tree(target, attempts=2)

    assert target.exists()


def test_what_was_left_behind_is_reported(tmp_path, monkeypatch, caplog):
    """If this ever fires anyway, the contents are the whole diagnosis."""
    target = tmp_path / "scratch"
    target.mkdir()
    (target / "surprise.lock").write_text("x")

    monkeypatch.setattr(
        shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(OSError(39, "nope"))
    )

    with caplog.at_level(logging.WARNING):
        remove_tree(target, attempts=1)

    assert "surprise.lock" in caplog.text


@pytest.mark.parametrize("setting", ["gc.auto", "maintenance.auto"])
def test_background_maintenance_is_switched_off(tmp_path, setting):
    """The fix at the source: no detached writer, no race to tolerate."""
    repo = a_repo(tmp_path / "repo")

    quiet_git(repo)

    value = subprocess.run(
        ["git", "config", "--get", setting],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert value in ("0", "false")
