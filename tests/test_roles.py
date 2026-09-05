"""What a machine is, and everything that keys off the answer.

A head runs Workbench; a node lends it a GPU. The role is one fact, recorded
once by the installer, and several separate things read it. This file covers
the fact and its readers together, because the failure worth guarding against
is them disagreeing — a node that installs the head's units is a machine with a
web service failing on a database that was never created.
"""

import pytest

from workbench import install
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
