"""What a machine is, and everything that keys off the answer.

A head runs Workbench; a node lends it a GPU. The role is one fact, recorded
once by the installer, and three separate things read it: which units belong
here, what a deploy should do, and which questions the doctor asks. This file
covers the fact and all three readers together, because the failure worth
guarding against is them disagreeing — a node that installs the head's units
is a machine with a web service failing on a database that was never created.
"""

import pytest

from workbench import deploy, doctor, install
from workbench.config import ROLE_HEAD, ROLE_NODE, is_node, role, role_marker


@pytest.fixture
def marker(tmp_path, monkeypatch):
    """A `data/` of this test's own, so the marker is this test's marker."""
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "workbench.db"))
    monkeypatch.delenv("WORKBENCH_ROLE", raising=False)
    path = role_marker()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_a_machine_with_no_marker_is_a_head(marker):
    """Every install that predates roles is a head, and a laptop checkout
    should behave as one without being told."""
    assert role() == ROLE_HEAD
    assert not is_node()


def test_the_marker_is_what_makes_a_node(marker):
    marker.write_text("node\n")

    assert role() == ROLE_NODE
    assert is_node()


def test_the_environment_beats_the_marker(marker, monkeypatch):
    """So a person can ask "what would this look like as a node" without
    reinstalling, which is how the node checks get exercised at all here."""
    marker.write_text("head\n")
    monkeypatch.setenv("WORKBENCH_ROLE", "node")

    assert role() == ROLE_NODE


def test_an_unrecognised_role_falls_back_to_head_and_says_so(marker, caplog):
    """Quietly treating a typo as a node would install the wrong units."""
    marker.write_text("nodee\n")

    with caplog.at_level("WARNING"):
        assert role() == ROLE_HEAD

    assert "nodee" in caplog.text


def test_a_head_installs_the_app_the_deployer_and_the_run_template(marker):
    names = {unit for unit, _template in install.units()}

    assert "workbench.service" in names
    assert "workbench-deploy.timer" in names
    assert "workbench-run@.service" in names


def test_a_node_installs_the_deployer_and_nothing_else(marker):
    """No app, because there is no database to serve; no run template, because
    runs execute on the head. Only the timer, which is not optional: a node
    that cannot update itself is a manual step for as long as it exists."""
    marker.write_text("node\n")

    names = {unit for unit, _template in install.units()}

    assert names == {"workbench-deploy.service", "workbench-deploy.timer"}


def test_a_node_is_asked_about_its_gpu_and_its_model_server(marker):
    marker.write_text("node\n")

    keys = {check.__name__ for check in doctor.checks_for_this_machine()}

    assert "check_gpu" in keys
    assert "check_inference_endpoint" in keys


def test_a_node_is_not_asked_about_work_it_does_not_do(marker):
    """A report full of correct, unactionable failures is one people skim."""
    marker.write_text("node\n")

    keys = {check.__name__ for check in doctor.checks_for_this_machine()}

    assert "check_deploy_key" not in keys
    assert "check_github_token" not in keys
    assert "check_agent_credential" not in keys
    assert "check_tailscale_serve" not in keys


def test_a_head_is_asked_the_list_it_always_was(marker):
    assert doctor.checks_for_this_machine() == doctor.HEAD_CHECKS


def test_a_node_deploy_does_not_migrate_a_database_it_has_not_got(monkeypatch, tmp_path):
    """The node's `data/` holds a role marker and nothing else. Running
    migrations there would create a schema nothing reads, and `alembic check`
    would then be deciding whether a deploy succeeded on a machine that has no
    stake in the answer."""
    monkeypatch.setenv("WORKBENCH_ROLE", "node")
    commands: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(deploy, "_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(
        deploy, "_run", lambda argv, **_kwargs: (commands.append(argv), Completed())[1]
    )
    monkeypatch.setattr(deploy, "refresh_units", lambda: None)
    monkeypatch.setattr(deploy, "converge_inference_server", lambda: None)
    monkeypatch.setattr(
        deploy, "restart_service", lambda: pytest.fail("a node restarted an app it never installed")
    )

    assert deploy.rebuild_and_restart() is None
    assert not any("alembic" in " ".join(argv) for argv in commands)


def test_a_node_deploy_still_syncs_and_converges_its_units(monkeypatch):
    """The two things that must keep happening: the venv the deploy timer runs
    out of, and the units — which is how a change to either reaches a node."""
    monkeypatch.setenv("WORKBENCH_ROLE", "node")
    refreshed: list[int] = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(deploy, "_uv", lambda: "/usr/bin/uv")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        deploy, "_run", lambda argv, **_kwargs: (commands.append(argv), Completed())[1]
    )
    monkeypatch.setattr(deploy, "refresh_units", lambda: refreshed.append(1))
    monkeypatch.setattr(deploy, "converge_inference_server", lambda: None)

    deploy.rebuild_and_restart()

    assert refreshed == [1]
    assert any("sync" in " ".join(argv) for argv in commands)


def test_a_broken_model_server_does_not_fail_a_deploy(monkeypatch, caplog):
    """The node is still updated and still reachable. A model server needing
    attention is a thing to say, not a reason to leave a checkout half
    deployed at 3am with nobody watching."""

    def explode() -> bool:
        raise RuntimeError("ollama is not installed")

    monkeypatch.setattr("workbench.install_node.install_inference_server", explode)

    with caplog.at_level("WARNING"):
        assert deploy.converge_inference_server() is None

    assert "ollama is not installed" in caplog.text
