# Tests for named Claude logins functionality.


import pytest

from workbench.config import claude_login_dir, claude_login_names


@pytest.fixture
def tmp_workbench(tmp_path):
    return tmp_path


def test_login_names_empty_when_no_directory(tmp_workbench, monkeypatch):
    monkeypatch.setattr("workbench.config.agent_home", lambda: tmp_workbench)
    assert claude_login_names() == []


def test_login_names_lists_and_sorts_directories(tmp_workbench, monkeypatch):
    monkeypatch.setattr("workbench.config.agent_home", lambda: tmp_workbench)
    logs_dir = tmp_workbench / ".claude-logins"
    logs_dir.mkdir()
    (logs_dir / "z@example.com").mkdir()
    (logs_dir / "a@example.com").mkdir()
    assert claude_login_names() == ["a@example.com", "z@example.com"]


def test_login_dir_shows_existing_and_missing(tmp_workbench, monkeypatch):
    monkeypatch.setattr("workbench.config.agent_home", lambda: tmp_workbench)
    logs_dir = tmp_workbench / ".claude-logins"
    logs_dir.mkdir()
    existing = logs_dir / "a@example.com"
    existing.mkdir()
    assert claude_login_dir("a@example.com") == existing
    assert claude_login_dir("nope@here.com") is None
    assert claude_login_dir("../x") is None
