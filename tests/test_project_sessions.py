"""One standing remote-control session per project, as the project's own login.

What is worth pinning is *who* each session signs in as — the work account
for a project restricted to it, the default login for everything else — and
that the deployer only ever changes a session when its spec did, so a person's
live conversation is not restarted by an unrelated deploy.
"""

import json
import os
from pathlib import Path

import pytest

import workbench.deploy as deploy_module
from workbench import project_sessions, remote_control
from workbench.database.models import Project
from workbench.project_sessions import SessionSpec, read_spec, spec_for, write_specs
from workbench.runs.lifecycle import ClaudeLogin, project_claude_login

WORK = "ian@pivotrobotics.com"


@pytest.fixture(autouse=True)
def restored_environment():
    """The wrapper replaces `os.environ` wholesale, as its child will see it,
    which monkeypatch cannot undo."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def a_project(**fields) -> Project:
    return Project(owner="idm23", repo="workbench", github_url="u", **fields)


# --- whose login ----------------------------------------------------------


def test_an_unrestricted_project_uses_the_default_login():
    assert project_claude_login(a_project()) == ClaudeLogin(None)


def test_a_project_restricted_to_a_work_login_uses_it():
    """Pivot today: the only way it names the work account."""
    pivot = a_project(allowed_agents=[f"claude:{WORK}"])

    assert project_claude_login(pivot) == ClaudeLogin(WORK)


def test_a_project_whose_backend_names_a_login_uses_it():
    assert project_claude_login(a_project(agent_backend=f"claude:{WORK}")) == ClaudeLogin(WORK)


def test_a_backend_that_is_not_claude_still_leaves_the_default_login():
    """The workbench project runs on `claude` and executes with `local`; its
    session is still Claude, as the default login."""
    assert project_claude_login(a_project(agent_backend="local")) == ClaudeLogin(None)


def test_a_project_that_may_not_use_claude_gets_no_session():
    assert project_claude_login(a_project(allowed_agents=["local"])) is None


# --- the spec -------------------------------------------------------------


@pytest.fixture
def cloned(monkeypatch, tmp_path):
    clone = tmp_path / "repos" / "idm23-workbench"
    clone.mkdir(parents=True)
    monkeypatch.setattr(project_sessions, "local_checkout", lambda owner, repo: clone)
    monkeypatch.setattr(project_sessions.socket, "gethostname", lambda: "homebox-core")
    return clone


def test_a_default_login_spec_has_no_config_dir(cloned):
    assert spec_for(a_project()) == SessionSpec(
        name="workbench on homebox-core", cwd=str(cloned), config_dir=None
    )


def test_a_work_login_spec_points_at_its_directory(cloned, monkeypatch, tmp_path):
    login = tmp_path / ".claude-logins" / WORK
    monkeypatch.setattr(project_sessions, "claude_login_dir", lambda name: login)

    spec = spec_for(a_project(allowed_agents=[f"claude:{WORK}"]))

    assert spec is not None
    assert spec.config_dir == str(login)


def test_a_login_never_created_here_gets_no_session(cloned, monkeypatch):
    """It would only sit at the sign-in screen."""
    monkeypatch.setattr(project_sessions, "claude_login_dir", lambda name: None)

    assert spec_for(a_project(allowed_agents=[f"claude:{WORK}"])) is None


def test_a_project_not_cloned_here_gets_no_session(monkeypatch):
    monkeypatch.setattr(project_sessions, "local_checkout", lambda owner, repo: None)

    assert spec_for(a_project()) is None


def test_writing_specs_drops_a_removed_projects_and_round_trips():
    one = SessionSpec(name="a", cwd="/a", config_dir=None)
    two = SessionSpec(name="b", cwd="/b", config_dir="/logins/w")
    write_specs({1: one, 2: two})

    write_specs({2: two})

    assert read_spec(1) is None
    assert read_spec(2) == two


def test_a_fingerprint_moves_with_the_login_and_nothing_else():
    spec = SessionSpec(name="a", cwd="/a", config_dir=None)

    assert spec.fingerprint() == SessionSpec(name="a", cwd="/a", config_dir=None).fingerprint()
    assert spec.fingerprint() != SessionSpec(name="a", cwd="/a", config_dir="/w").fingerprint()


# --- the session itself ---------------------------------------------------


def test_a_project_session_starts_in_its_clone_as_its_login(monkeypatch, tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    login = tmp_path / "work"
    write_specs(
        {3: SessionSpec(name="pivot on homebox-core", cwd=str(clone), config_dir=str(login))}
    )
    real_database = os.environ["WORKBENCH_DB"]
    monkeypatch.chdir(tmp_path)
    seen = {}

    def spawn(argv):
        seen.update(argv=argv, cwd=str(Path.cwd()), env=dict(os.environ))
        return 0

    monkeypatch.setattr(remote_control, "remote_control_argv", lambda name=None: ["claude", name])
    monkeypatch.setattr(remote_control.pty, "spawn", spawn)

    assert remote_control.main(["--project", "3"]) == 0
    assert seen["argv"] == ["claude", "pivot on homebox-core"]
    assert seen["cwd"] == str(clone)
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(login)
    assert seen["env"]["WORKBENCH_DB"] != real_database
    assert json.loads((login / ".claude.json").read_text())["projects"][str(clone)][
        "hasTrustDialogAccepted"
    ]


def test_a_default_login_session_unsets_a_config_dir(monkeypatch, tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    write_specs({1: SessionSpec(name="workbench", cwd=str(clone), config_dir=None)})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
    seen = {}
    monkeypatch.setattr(remote_control, "remote_control_argv", lambda name=None: ["claude"])
    monkeypatch.setattr(
        remote_control.pty, "spawn", lambda argv: seen.update(env=dict(os.environ)) or 0
    )

    assert remote_control.main(["--project", "1"]) == 0
    assert "CLAUDE_CONFIG_DIR" not in seen["env"]
    assert (tmp_path / ".claude.json").exists()


def test_a_session_without_a_spec_does_not_start(monkeypatch):
    monkeypatch.setattr(remote_control.pty, "spawn", lambda argv: pytest.fail("started"))

    assert remote_control.main(["--project", "9"]) == 1


# --- the deployer ---------------------------------------------------------


class _Ran:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def systemctl(monkeypatch, tmp_path):
    """The deployer against a fake `systemctl`, wanting whatever `wanted` holds."""
    monkeypatch.setattr(deploy_module, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(deploy_module, "ensure_data_dir", lambda: None)
    monkeypatch.setattr(deploy_module, "restore_from", lambda: None)
    wanted: dict[str, str] = {}
    ran: list[list[str]] = []

    def run(argv, **kwargs):
        if "workbench.project_sessions" in argv:
            return _Ran(stdout="a warning line\n" + json.dumps(wanted) + "\n")
        ran.append(argv)
        if argv[1] == "show":
            return _Ran(stdout="ActiveState=active\nUnitFileState=enabled\n")
        return _Ran()

    monkeypatch.setattr(deploy_module, "_run", run)
    return wanted, ran


def unit(project_id: int) -> str:
    return f"workbench-project-session@{project_id}.service"


def test_a_new_project_gets_its_session_started(systemctl):
    wanted, ran = systemctl
    wanted.update({"1": "aaa", "3": "bbb"})

    deploy_module.converge_project_sessions()

    assert ran == [
        ["systemctl", "enable", "--now", unit(1)],
        ["systemctl", "enable", "--now", unit(3)],
    ]


def test_an_unchanged_session_is_not_restarted(systemctl):
    """It may be somebody's live conversation."""
    wanted, ran = systemctl
    wanted.update({"1": "aaa"})
    deploy_module.converge_project_sessions()
    ran.clear()

    deploy_module.converge_project_sessions()

    assert ran == [["systemctl", "show", unit(1), "--property=ActiveState,UnitFileState"]]


def test_a_changed_login_restarts_the_session(systemctl):
    wanted, ran = systemctl
    wanted.update({"3": "default-login"})
    deploy_module.converge_project_sessions()
    ran.clear()
    wanted["3"] = "work-login"

    deploy_module.converge_project_sessions()

    assert ran == [["systemctl", "try-restart", unit(3)]]


def test_a_removed_project_has_its_session_stopped(systemctl):
    wanted, ran = systemctl
    wanted.update({"1": "aaa", "2": "bbb"})
    deploy_module.converge_project_sessions()
    ran.clear()
    del wanted["2"]

    deploy_module.converge_project_sessions()

    assert ["systemctl", "disable", "--now", unit(2)] in ran


def test_staging_starts_no_project_sessions(systemctl, monkeypatch, tmp_path):
    wanted, ran = systemctl
    wanted.update({"1": "aaa"})
    monkeypatch.setattr(deploy_module, "restore_from", lambda: tmp_path / "production.db")

    deploy_module.converge_project_sessions()

    assert ran == []
