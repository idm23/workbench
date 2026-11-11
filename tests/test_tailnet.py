"""Joining the home tailnet, the external role, and what the doctor says about both.

The failure all of this exists for is a browser asking for a login on a machine
nobody is looking at — because its key expired on a clock nothing was watching,
or because joining it was a step a person had to remember.
"""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from workbench import doctor, install, install_external, tailnet
from workbench.config import ROLE_EXTERNAL, is_external, role_marker, ssh_hosts
from workbench.doctor import CheckState


def status(**overrides) -> tailnet.TailnetStatus:
    fields = {
        "backend_state": "Running",
        "hostname": "homebox-core",
        "tailnet": "idm@",
        "tags": (),
        "key_expiry": None,
        "peers": {"homebox-node-1": True, "homebox-node-2": False, "iphone": True},
    }
    fields.update(overrides)
    return tailnet.TailnetStatus(**fields)


# --- Reading a daemon ---------------------------------------------------------


def test_status_reads_tags_expiry_and_peers():
    parsed = tailnet.parse_status(
        {
            "BackendState": "Running",
            "CurrentTailnet": {"Name": "idm@"},
            "Self": {"HostName": "homebox-core", "KeyExpiry": "2027-03-01T12:00:00Z"},
            "Peer": {
                "a": {"HostName": "homebox-node-1", "Online": True},
                "b": {"HostName": "homebox-node-2", "Online": False},
            },
        }
    )

    assert parsed.running
    assert parsed.key_expiry == datetime(2027, 3, 1, 12, tzinfo=UTC)
    assert parsed.peers == {"homebox-node-1": True, "homebox-node-2": False}


def test_a_tagged_device_has_no_expiry():
    parsed = tailnet.parse_status(
        {"BackendState": "Running", "Self": {"HostName": "n", "Tags": ["tag:workbench"]}}
    )

    assert parsed.key_expiry is None
    assert parsed.tags == ("tag:workbench",)


def test_a_daemon_that_is_not_there_says_why(monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(tailnet.subprocess, "run", missing)

    assert tailnet.read_status() == "tailscale is not installed."


# --- Joining ------------------------------------------------------------------


def test_the_key_comes_from_the_flag_or_the_environment(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["x", "--tailscale-auth-key", "tskey-flag"])
    assert tailnet.auth_key() == "tskey-flag"

    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.setenv(tailnet.AUTH_KEY_SETTING, "tskey-env")
    assert tailnet.auth_key() == "tskey-env"


def test_joining_is_tagged_and_turns_on_ssh(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["x", "--tailscale-tag=home"])

    argv = tailnet.up_command("tskey-abc")

    assert argv == ["tailscale", "up", "--auth-key=tskey-abc", "--advertise-tags=tag:home", "--ssh"]


def test_a_refused_key_is_never_echoed(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.setattr(
        tailnet.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "invalid key tskey-secret"),
    )

    outcome = tailnet.join("tskey-secret")

    assert isinstance(outcome, tailnet.NotJoined)
    assert "tskey-secret" not in outcome.reason


def test_an_install_never_reauthenticates_a_joined_machine(monkeypatch):
    """Re-authenticating over a session that rides the tailnet cuts it off."""
    monkeypatch.setattr(sys, "argv", ["x", "--tailscale-auth-key=tskey-abc"])
    monkeypatch.setattr(install.tailnet, "read_status", lambda *_a: status())
    ran = []
    monkeypatch.setattr(
        install.subprocess,
        "run",
        lambda argv, **_k: ran.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(
        install.tailnet, "join", lambda *_a, **_k: pytest.fail("a joined machine was rejoined")
    )

    install.ensure_tailnet()

    assert ran == [["tailscale", "set", "--ssh"]]


def test_an_install_with_a_key_joins_a_machine_that_is_off(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["x", "--tailscale-auth-key=tskey-abc"])
    monkeypatch.setattr(install.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(
        install.tailnet, "read_status", lambda *_a: status(backend_state="NeedsLogin")
    )
    monkeypatch.setattr(install, "systemd_is_running", lambda: True)
    spent = []
    monkeypatch.setattr(
        install.tailnet,
        "join",
        lambda key, **_k: spent.append(key) or tailnet.Joined(status(tags=("tag:workbench",))),
    )

    install.ensure_tailnet()

    assert spent == ["tskey-abc"]


def test_an_install_without_a_key_leaves_joining_to_a_person(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.delenv(tailnet.AUTH_KEY_SETTING, raising=False)
    monkeypatch.setattr(install.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(
        install.tailnet, "read_status", lambda *_a: status(backend_state="NeedsLogin")
    )
    monkeypatch.setattr(
        install.tailnet, "join", lambda *_a, **_k: pytest.fail("joined with no key")
    )

    install.ensure_tailnet()


# --- The doctor ---------------------------------------------------------------


def test_a_key_that_never_expires_is_fine(monkeypatch):
    monkeypatch.setattr(doctor.tailnet, "read_status", lambda *_a: status(tags=("tag:workbench",)))

    check = doctor.check_tailnet()

    assert check.state is CheckState.OK
    assert "never expires" in check.detail


def test_a_key_about_to_expire_says_how_to_stop_it_happening(monkeypatch):
    soon = datetime.now(UTC) + timedelta(days=5)
    monkeypatch.setattr(doctor.tailnet, "read_status", lambda *_a: status(key_expiry=soon))

    check = doctor.check_tailnet()

    assert check.state is CheckState.WARN
    assert "Disable key expiry" in check.detail


def test_an_expired_key_fails_with_the_login(monkeypatch):
    gone = datetime.now(UTC) - timedelta(days=1)
    monkeypatch.setattr(doctor.tailnet, "read_status", lambda *_a: status(key_expiry=gone))

    check = doctor.check_tailnet()

    assert check.state is CheckState.FAIL
    assert check.fix and "tailscale" in check.fix


def test_no_tailscale_on_a_head_is_unknown_not_a_failure(monkeypatch):
    """The fresh-install container has none, and must not go red for it."""
    monkeypatch.setattr(doctor.tailnet, "read_status", lambda *_a: "tailscale is not installed.")
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    assert doctor.check_tailnet().state is CheckState.UNKNOWN


# --- The external role --------------------------------------------------------


def test_an_external_machine_is_asked_only_how_it_gets_home():
    role_marker().parent.mkdir(parents=True, exist_ok=True)
    role_marker().write_text(f"{ROLE_EXTERNAL}\n")

    assert is_external()
    assert doctor.checks_for_this_machine() == doctor.EXTERNAL_CHECKS


def test_the_external_install_refuses_root(monkeypatch):
    monkeypatch.setattr(install_external.os, "geteuid", lambda: 0)

    with pytest.raises(install.InstallError, match="not as root"):
        install_external.check_not_root()


def test_recording_hosts_is_read_back(monkeypatch):
    monkeypatch.delenv("WORKBENCH_SSH_HOSTS", raising=False)

    install_external.record(["homebox-*", "other"])

    assert ssh_hosts() == ["homebox-*", "other"]


def test_the_ssh_block_goes_first_and_replaces_itself():
    mine = "Host github.com\n  AddKeysToAgent yes\n"
    block = install_external.ssh_block(["homebox-*"])

    once = install_external.with_block(mine, block)
    twice = install_external.with_block(once, install_external.ssh_block(["homebox-*", "x"]))

    assert once.startswith(install_external.SSH_BLOCK_START)
    assert once.endswith(mine)
    assert twice.count(install_external.SSH_BLOCK_START) == 1
    assert "Host homebox-* x" in twice
    assert twice.endswith(mine)


def test_the_route_check_names_what_it_reaches(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKBENCH_SSH_HOSTS", "homebox-*")
    config = tmp_path / "config"
    config.write_text(install_external.ssh_block(["homebox-*"]))
    monkeypatch.setattr(install_external, "ssh_config_path", lambda: Path(config))
    monkeypatch.setattr(doctor.tailnet, "read_status", lambda *_a: status())

    check = doctor.check_home_ssh_route()

    assert check.state is CheckState.OK
    assert check.detail == "Reaches homebox-node-1."
