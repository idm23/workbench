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
from workbench.config import (
    GAMING,
    INFERENCE,
    ROLE_HEAD,
    ROLE_NODE,
    capabilities_marker,
    declared_capabilities,
    gaming_unit_name,
    is_gaming_node,
    is_node,
    role,
    role_marker,
)


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


def test_a_head_is_asked_the_list_it_always_was(marker, monkeypatch):
    monkeypatch.delenv("WORKBENCH_AGENT_BACKEND", raising=False)

    assert doctor.checks_for_this_machine() == doctor.HEAD_CHECKS


def test_a_head_that_uses_a_local_model_is_asked_about_its_nodes(marker, monkeypatch):
    """And only then. Asking unconditionally would put a warning about worker
    nodes on every machine that has never wanted one."""
    monkeypatch.setenv("WORKBENCH_AGENT_BACKEND", "local")

    names = [check.__name__ for check in doctor.checks_for_this_machine()]

    assert names[-1] == "check_inference_node"


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
    monkeypatch.setattr(deploy, "converge_node", lambda: None)
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
    monkeypatch.setattr(deploy, "converge_node", lambda: None)

    deploy.rebuild_and_restart()

    assert refreshed == [1]
    assert any("sync" in " ".join(argv) for argv in commands)


def test_a_node_reregisters_on_every_deploy_tick(monkeypatch):
    """Not only when something was pulled. `nodes.last_seen_at` is the one
    failure a head can notice by itself — nothing polls — so a node that is
    alive has to keep saying so."""
    monkeypatch.setenv("WORKBENCH_ROLE", "node")
    registered: list[int] = []
    monkeypatch.setattr("workbench.install_node.install_inference_server", lambda: True)
    monkeypatch.setattr("workbench.install_node.register_with_head", lambda: registered.append(1))

    assert deploy.converge_node() is None
    assert registered == [1]


def test_a_broken_model_server_does_not_fail_a_deploy(monkeypatch, caplog):
    """The node is still updated and still reachable. A model server needing
    attention is a thing to say, not a reason to leave a checkout half
    deployed at 3am with nobody watching."""

    def explode() -> bool:
        raise RuntimeError("ollama is not installed")

    monkeypatch.setattr("workbench.install_node.install_inference_server", explode)
    monkeypatch.setattr("workbench.install_node.register_with_head", lambda: None)

    with caplog.at_level("WARNING"):
        assert deploy.converge_node() is None

    assert "ollama is not installed" in caplog.text


@pytest.fixture
def offering(tmp_path, monkeypatch):
    """A `data/capabilities` of this test's own. See `marker` above."""
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "workbench.db"))
    monkeypatch.delenv("WORKBENCH_CAPABILITIES", raising=False)
    path = capabilities_marker()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_a_node_with_no_capabilities_marker_serves_models(offering):
    """Every node installed before this file existed does exactly that, and
    must keep doing it without anyone re-running the installer."""
    assert declared_capabilities() == [INFERENCE]


def test_a_node_declares_what_it_was_installed_for(offering):
    offering.write_text("inference,gaming\n")

    assert declared_capabilities() == [INFERENCE, GAMING]


def test_a_declaration_may_leave_out_inference(offering):
    """A machine bought to drive a TV lends the head a GPU it must never
    dispatch a model to."""
    offering.write_text("gaming\n")

    assert declared_capabilities() == [GAMING]


def test_an_unknown_capability_is_ignored_and_said_out_loud(offering, caplog):
    """Warned rather than refused, unlike the installer's flag: this file is
    already on a machine, and a node that stops registering over a typo is
    worse than one that does less than someone thinks."""
    offering.write_text("inference,minecraft\n")

    with caplog.at_level("WARNING"):
        assert declared_capabilities() == [INFERENCE]

    assert "minecraft" in caplog.text


def test_the_environment_beats_the_capabilities_marker(offering, monkeypatch):
    offering.write_text("inference\n")
    monkeypatch.setenv("WORKBENCH_CAPABILITIES", "gaming")

    assert declared_capabilities() == [GAMING]


def test_a_gaming_node_also_gets_the_switch(offering, monkeypatch):
    """Rendered by `units()` rather than by the gaming installer, so a deploy
    converges it. A unit only the installer writes is a unit a machine updated
    by the timer never gets, which this project has learned three times."""
    monkeypatch.setenv("WORKBENCH_ROLE", "node")
    offering.write_text("inference,gaming\n")

    names = [name for name, _ in install.units()]

    assert f"{gaming_unit_name()}.service" in names
    # Still a node: no app, no run template.
    assert not any(name.startswith("workbench.service") for name in names)


def test_a_plain_node_gets_no_switch(offering, monkeypatch):
    monkeypatch.setenv("WORKBENCH_ROLE", "node")
    offering.write_text("inference\n")

    assert f"{gaming_unit_name()}.service" not in [name for name, _ in install.units()]


def test_a_head_gets_no_switch_however_it_is_declared(offering, monkeypatch):
    """The switch is about lending a GPU elsewhere. A head has nowhere to lend
    it to, and `is_gaming_node` says so rather than the capability alone."""
    monkeypatch.setenv("WORKBENCH_ROLE", "head")
    offering.write_text("inference,gaming\n")

    assert not is_gaming_node()
    assert f"{gaming_unit_name()}.service" not in [name for name, _ in install.units()]


def test_a_deploy_does_not_take_the_gpu_back_mid_game(monkeypatch, caplog):
    """The one thing a five-minute timer must not converge unannounced.

    Rewriting the drop-in restarts ollama, which takes the VRAM back from
    underneath whoever is playing. The next idle tick converges it instead —
    deciding from state, which is what these ticks already do.
    """
    converged: list[int] = []
    registered: list[int] = []
    monkeypatch.setattr(deploy, "gpu_is_busy_elsewhere", lambda: True)
    monkeypatch.setattr(
        "workbench.install_node.install_inference_server", lambda: converged.append(1)
    )
    monkeypatch.setattr("workbench.install_node.register_with_head", lambda: registered.append(1))

    with caplog.at_level("INFO"):
        assert deploy.converge_node() is None

    assert converged == []
    # Still heartbeats, and that is the point: a busy node is not a gone node.
    assert registered == [1]
    assert gaming_unit_name() in caplog.text


def test_an_idle_node_converges_normally(monkeypatch):
    converged: list[int] = []
    monkeypatch.setattr(deploy, "gpu_is_busy_elsewhere", lambda: False)
    monkeypatch.setattr(
        "workbench.install_node.install_inference_server", lambda: converged.append(1)
    )
    monkeypatch.setattr("workbench.install_node.register_with_head", lambda: None)

    assert deploy.converge_node() is None
    assert converged == [1]


def test_a_machine_without_systemd_is_never_busy(monkeypatch):
    """The container the fresh-install test runs in, and any laptop checkout."""
    monkeypatch.setattr("workbench.install.systemd_is_running", lambda: False)

    assert deploy.gpu_is_busy_elsewhere() is False


def test_not_being_able_to_ask_converges_rather_than_stalls(monkeypatch, caplog):
    """Not knowing is not the same as knowing it is idle — but a node that
    stopped updating over a transient would be worse than a restarted model
    server. Converge, and say why."""
    monkeypatch.setattr("workbench.install.systemd_is_running", lambda: True)

    def missing(*args, **kwargs):
        raise OSError("systemctl vanished")

    monkeypatch.setattr(deploy.subprocess, "run", missing)

    with caplog.at_level("WARNING"):
        assert deploy.gpu_is_busy_elsewhere() is False

    assert "systemctl vanished" in caplog.text
