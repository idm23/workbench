"""The node installer's own decisions, without installing anything.

Everything here is a step that talks to the machine — a driver probe, a
package installer, a systemd drop-in — so what can be tested is the shape of
what it would do and, more usefully, what it does when the machine cannot
oblige. The container the fresh-install test runs in is exactly that case, and
so is a laptop with no GPU: both have to finish the install and say what is
missing rather than fail.
"""

import json
import os
import pwd

import pytest

from workbench import install_node
from workbench.install import InstallError


def test_the_drop_in_binds_every_interface(monkeypatch):
    """The head reaches its node over the LAN and, failing that, the tailnet.
    OLLAMA_HOST takes one address, so binding both means binding all."""
    rendered = install_node._drop_in()

    assert "OLLAMA_HOST=0.0.0.0:11434" in rendered
    assert "[Service]" in rendered
    assert "OLLAMA_KEEP_ALIVE" in rendered


def test_the_drop_in_says_it_is_generated():
    """Someone will edit it on the machine, and it is rewritten on deploy."""
    assert "overwritten" in install_node._drop_in()


def test_no_systemd_skips_the_server_rather_than_failing(monkeypatch, caplog):
    """A container has nothing to manage and nothing to start. The install
    still has to finish and say what a real node would have got — this is the
    path `scripts/test_fresh_install.py` takes."""
    monkeypatch.setattr(install_node, "systemd_is_running", lambda: False)
    monkeypatch.setattr(
        install_node, "run", lambda *a, **k: pytest.fail("it tried to install Ollama anyway")
    )

    with caplog.at_level("INFO"):
        assert install_node.install_inference_server() is False

    assert "Skipping the model server" in caplog.text


def test_a_failed_ollama_install_names_the_command_to_run(monkeypatch):
    """The one genuinely fatal step: without a server there is no node."""

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "curl: (7) failed to connect"

    monkeypatch.setattr(install_node, "systemd_is_running", lambda: True)
    monkeypatch.setattr(install_node.shutil, "which", lambda _name: None)
    monkeypatch.setattr(install_node, "run", lambda *a, **k: Failed())

    with pytest.raises(InstallError) as raised:
        install_node.install_inference_server()

    assert "ollama.com/install.sh" in str(raised.value)


def test_a_model_that_will_not_pull_is_a_warning_not_a_failure(monkeypatch, caplog):
    """The node is installed either way, and the fix is one command — better
    said here than by a run failing days later."""

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "no space left on device"

    monkeypatch.setenv("WORKBENCH_LOCAL_MODEL", "qwen2.5-coder:7b")
    monkeypatch.setattr(install_node, "run", lambda *a, **k: Failed())

    with caplog.at_level("INFO"):
        install_node.pull_model()

    assert "ollama pull qwen2.5-coder:7b" in caplog.text


def test_a_missing_driver_is_reported_with_the_command(monkeypatch, caplog):
    """Never a failure: a node with no GPU still serves, on the CPU, slowly.
    What it must not be is silent about why."""
    monkeypatch.setattr(install_node.shutil, "which", lambda _name: None)

    with caplog.at_level("INFO"):
        install_node.check_gpu()

    assert "run on the CPU" in caplog.text
    assert "ubuntu-drivers install" in caplog.text


def test_addresses_are_offered_lan_first(monkeypatch):
    """The order is the message: the head probes this list top down, so the
    route that costs one hop has to come before the one that costs WireGuard
    and a coordination server."""

    class Addresses:
        returncode = 0
        stdout = "100.120.132.42 192.168.1.155 172.17.0.1 fe80::1 127.0.0.1\n"
        stderr = ""

    monkeypatch.setattr(install_node.subprocess, "run", lambda *a, **k: Addresses())

    assert install_node.addresses() == ["192.168.1.155", "100.120.132.42"]


def test_addresses_nothing_can_route_to_are_left_out(monkeypatch):
    """Docker's bridge is reachable from nowhere but this machine, and a
    link-local address is worse than useless to a head: it resolves, and then
    it does not work."""

    class Addresses:
        returncode = 0
        stdout = "172.17.0.1 169.254.3.4 127.0.0.1\n"
        stderr = ""

    monkeypatch.setattr(install_node.subprocess, "run", lambda *a, **k: Addresses())

    assert install_node.addresses() == []


def test_the_head_flag_is_read_in_either_spelling(monkeypatch):
    for argv in (
        ["--role=node", "--head", "http://homebox-core:8787"],
        ["--head=http://homebox-core:8787/", "--role=node"],
    ):
        monkeypatch.setattr(install_node.sys, "argv", ["install", *argv])
        assert install_node._head_argument() == "http://homebox-core:8787"


def test_no_head_flag_is_not_an_error(monkeypatch):
    """A node installed without one still serves models. It is simply
    invisible until someone points a head at it."""
    monkeypatch.setattr(install_node.sys, "argv", ["install", "--role=node"])

    assert install_node._head_argument() is None


def test_an_unregistered_node_says_how_to_register(monkeypatch, caplog):
    monkeypatch.delenv("WORKBENCH_HEAD_URL", raising=False)
    monkeypatch.setattr(install_node, "head_url", lambda: None)

    with caplog.at_level("INFO"):
        install_node.register_with_head()

    assert "--head" in caplog.text


def test_a_head_that_is_off_does_not_fail_the_install(monkeypatch, caplog):
    """The node still serves models, and its deploy timer tries again in five
    minutes. What it must not do is stay quiet about having failed."""
    monkeypatch.setattr(install_node, "head_url", lambda: "http://homebox-core:8787")
    monkeypatch.setattr(install_node, "addresses", lambda: ["192.168.1.155"])
    monkeypatch.setattr(install_node, "gpu_description", lambda: None)

    def refuse(*args, **kwargs):
        raise install_node.urllib.error.URLError("connection refused")

    monkeypatch.setattr(install_node.urllib.request, "urlopen", refuse)

    with caplog.at_level("INFO"):
        install_node.register_with_head()

    assert "could not register" in caplog.text
    assert "try again" in caplog.text


def test_the_lan_address_is_preferred_over_the_tailnet_one(monkeypatch):
    """The hint printed at the end of an install. The LAN address is the direct
    route between two machines in the same house; the tailnet one is the
    fallback, and printing it here would send the head the long way round."""

    class Addresses:
        returncode = 0
        stdout = "100.115.84.59 192.168.1.153 fe80::1\n"
        stderr = ""

    monkeypatch.setattr(install_node.subprocess, "run", lambda *a, **k: Addresses())

    assert install_node._lan_address() == "192.168.1.153"


def test_no_address_is_better_than_a_wrong_one(monkeypatch):
    class Nothing:
        returncode = 0
        stdout = "100.115.84.59\n"
        stderr = ""

    monkeypatch.setattr(install_node.subprocess, "run", lambda *a, **k: Nothing())

    assert install_node._lan_address() is None


def test_the_capabilities_flag_is_read_in_either_spelling(monkeypatch):
    for argv in (
        ["--role=node", "--capabilities", "inference,gaming"],
        ["--capabilities=inference, gaming", "--role=node"],
    ):
        monkeypatch.setattr(install_node.sys, "argv", ["install", *argv])
        assert install_node._capabilities_argument() == ["inference", "gaming"]


def test_no_capabilities_flag_leaves_the_declaration_alone(monkeypatch):
    """A re-install that says nothing must offer exactly what it offered
    before, the way `--head` does. None is 'do not touch', not 'the default'."""
    monkeypatch.setattr(install_node.sys, "argv", ["install", "--role=node"])

    assert install_node._capabilities_argument() is None


def test_a_misspelled_capability_is_refused_rather_than_dropped(monkeypatch):
    """Reading the marker warns and ignores, because a machine that already has
    one must keep working. Typing the flag now is different: silently accepting
    it would hand someone a node that serves no models and says nothing."""
    monkeypatch.setattr(install_node.sys, "argv", ["install", "--capabilities=gaming,infrence"])

    with pytest.raises(InstallError) as refused:
        install_node._capabilities_argument()

    assert "infrence" in str(refused.value)
    assert "inference" in str(refused.value)  # names what it should have been


def test_a_node_offers_what_it_declared_when_the_server_answers(monkeypatch):
    monkeypatch.setattr(install_node, "declared_capabilities", lambda: ["inference", "gaming"])
    monkeypatch.setattr(install_node, "_endpoint_answers", lambda *a, **k: True)

    assert install_node.capabilities() == ["inference", "gaming"]


def test_a_node_withdraws_inference_when_its_server_is_silent(monkeypatch):
    """The whole feature, in one assertion. A game has the card, or ollama
    died, or the disk filled — the head must stop sending runs either way."""
    monkeypatch.setattr(install_node, "declared_capabilities", lambda: ["inference", "gaming"])
    monkeypatch.setattr(install_node, "_endpoint_answers", lambda *a, **k: False)

    assert install_node.capabilities() == ["gaming"]


def test_a_plain_node_with_a_dead_server_offers_nothing(monkeypatch):
    """An empty list rather than a stale claim. This is the case that used to
    be wrong: the node went on advertising inference, and every run paid a
    probe and then failed."""
    monkeypatch.setattr(install_node, "declared_capabilities", lambda: ["inference"])
    monkeypatch.setattr(install_node, "_endpoint_answers", lambda *a, **k: False)

    assert install_node.capabilities() == []


def test_a_node_that_never_serves_models_is_not_probed(monkeypatch):
    """A gaming-only node should not pay a loopback timeout on every tick."""
    monkeypatch.setattr(install_node, "declared_capabilities", lambda: ["gaming"])

    def fail(*args, **kwargs):
        raise AssertionError("probed the endpoint on a node that declared no inference")

    monkeypatch.setattr(install_node, "_endpoint_answers", fail)

    assert install_node.capabilities() == ["gaming"]


def test_registration_sends_what_is_offered_now_not_what_was_declared(monkeypatch):
    """The payload is the derived list. Sending the declared one would put the
    burden on the head to guess which half of it is currently true."""
    sent = {}

    monkeypatch.setattr(install_node, "head_url", lambda: "http://homebox-core:8787")
    monkeypatch.setattr(install_node, "addresses", lambda: ["192.168.1.155"])
    monkeypatch.setattr(install_node, "gpu_description", lambda: None)
    monkeypatch.setattr(install_node, "capabilities", lambda: ["gaming"])

    class Answer:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def capture(request, timeout=None):
        sent.update(install_node.json.loads(request.data))
        return Answer()

    monkeypatch.setattr(install_node.urllib.request, "urlopen", capture)
    install_node.register_with_head()

    assert sent["capabilities"] == ["gaming"]


def test_the_gaming_user_flag_is_read_in_either_spelling(monkeypatch):
    for argv in (
        ["--role=node", "--gaming-user", "ian"],
        ["--gaming-user=ian", "--role=node"],
    ):
        monkeypatch.setattr(install_node.sys, "argv", ["install", *argv])
        assert install_node._gaming_user_argument() == "ian"


def test_the_gaming_user_defaults_to_whoever_ran_sudo(monkeypatch):
    """Right almost always: the person running the installer on their own
    laptop is the person who will play on it."""
    monkeypatch.setattr(install_node.sys, "argv", ["install", "--role=node"])
    monkeypatch.setenv("SUDO_USER", "ian")

    assert install_node._gaming_user_argument() == "ian"


def test_no_sudo_user_and_no_flag_means_nobody(monkeypatch):
    """Already root, or a container. Returning None is what makes the polkit
    step skip with a warning — guessing uid 1000 would write a Sunshine config
    into the wrong home, and the symptom is a stream that connects and captures
    nothing, which looks like a Sunshine bug for an afternoon."""
    monkeypatch.setattr(install_node.sys, "argv", ["install", "--role=node"])
    monkeypatch.delenv("SUDO_USER", raising=False)

    assert install_node._gaming_user_argument() is None


def test_no_systemd_skips_the_switch_rather_than_failing(monkeypatch, caplog):
    """The container path, mirroring the model server's. It must finish the
    install and say what a real node would have got."""
    monkeypatch.setattr(install_node, "systemd_is_running", lambda: False)

    with caplog.at_level("INFO"):
        assert install_node.install_gaming() is False

    assert "hands its GPU to a game" in caplog.text


def test_steam_already_installed_is_left_alone(monkeypatch):
    monkeypatch.setattr(install_node.shutil, "which", lambda name: "/usr/bin/steam")
    monkeypatch.setattr(
        install_node, "run", lambda *a, **k: pytest.fail("should not touch apt at all")
    )

    assert install_node.install_steam() is True


def test_steam_install_enables_i386_only_when_not_already_enabled(monkeypatch):
    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    class Architectures:
        stdout = "amd64\n"

    calls = []
    monkeypatch.setattr(install_node.shutil, "which", lambda name: None)
    monkeypatch.setattr(install_node.subprocess, "run", lambda *a, **k: Architectures())
    monkeypatch.setattr(install_node, "run", lambda argv, **k: calls.append(argv) or Ok())

    assert install_node.install_steam() is True
    assert ["dpkg", "--add-architecture", "i386"] in calls
    assert ["apt-get", "install", "-y", install_node.STEAM_PACKAGE] in calls


def test_steam_install_skips_i386_when_already_enabled(monkeypatch):
    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    class Architectures:
        stdout = "i386\n"

    calls = []
    monkeypatch.setattr(install_node.shutil, "which", lambda name: None)
    monkeypatch.setattr(install_node.subprocess, "run", lambda *a, **k: Architectures())
    monkeypatch.setattr(install_node, "run", lambda argv, **k: calls.append(argv) or Ok())

    install_node.install_steam()

    assert ["dpkg", "--add-architecture", "i386"] not in calls


def test_a_failed_steam_install_is_a_warning_not_a_failure(monkeypatch, caplog):
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "no space left on device"

    class Architectures:
        stdout = "i386\n"

    monkeypatch.setattr(install_node.shutil, "which", lambda name: None)
    monkeypatch.setattr(install_node.subprocess, "run", lambda *a, **k: Architectures())
    monkeypatch.setattr(install_node, "run", lambda *a, **k: Failed())

    with caplog.at_level("WARNING"):
        assert install_node.install_steam() is False

    assert install_node.STEAM_PACKAGE in caplog.text


def test_steam_install_installs_the_i386_nvidia_libs_when_there_is_a_gpu(monkeypatch):
    """Found the hard way: without this, Steam's postinst blocks on an
    interactive debconf prompt asking for exactly this package."""

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    class Architectures:
        stdout = "i386\n"

    calls = []
    monkeypatch.setattr(
        install_node.shutil,
        "which",
        lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None,
    )
    monkeypatch.setattr(install_node.subprocess, "run", lambda *a, **k: Architectures())
    monkeypatch.setattr(install_node, "run", lambda argv, **k: calls.append(argv) or Ok())

    assert install_node.install_steam() is True
    assert ["apt-get", "install", "-y", install_node.NVIDIA_I386_PACKAGE] in calls
    # Before Steam itself, so Steam's own postinst never gets a chance to ask.
    assert calls.index(
        ["apt-get", "install", "-y", install_node.NVIDIA_I386_PACKAGE]
    ) < calls.index(["apt-get", "install", "-y", install_node.STEAM_PACKAGE])


def test_steam_install_skips_the_nvidia_libs_with_no_gpu(monkeypatch):
    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    class Architectures:
        stdout = "i386\n"

    calls = []
    monkeypatch.setattr(install_node.shutil, "which", lambda name: None)
    monkeypatch.setattr(install_node.subprocess, "run", lambda *a, **k: Architectures())
    monkeypatch.setattr(install_node, "run", lambda argv, **k: calls.append(argv) or Ok())

    install_node.install_steam()

    assert ["apt-get", "install", "-y", install_node.NVIDIA_I386_PACKAGE] not in calls


def test_a_failed_nvidia_i386_install_does_not_block_steam(monkeypatch, caplog):
    """The exact package name is a property of the driver series, which
    changes — a wrong guess must not cost the rest of the install."""

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    class Architectures:
        stdout = "i386\n"

    def fake_run(argv, **kwargs):
        if argv == ["apt-get", "install", "-y", install_node.NVIDIA_I386_PACKAGE]:
            raise InstallError("no such package")
        return Ok()

    monkeypatch.setattr(
        install_node.shutil,
        "which",
        lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None,
    )
    monkeypatch.setattr(install_node.subprocess, "run", lambda *a, **k: Architectures())
    monkeypatch.setattr(install_node, "run", fake_run)

    with caplog.at_level("WARNING"):
        assert install_node.install_steam() is True

    assert install_node.NVIDIA_I386_PACKAGE in caplog.text


def test_a_raising_steam_install_is_a_warning_not_a_crash(monkeypatch, caplog):
    """`install.run()` raises `InstallError` rather than returning a nonzero
    result, so this must be caught, not left to abort the whole installer."""

    class Architectures:
        stdout = "i386\n"

    def fake_run(argv, **kwargs):
        raise InstallError("no space left on device")

    monkeypatch.setattr(install_node.shutil, "which", lambda name: None)
    monkeypatch.setattr(install_node.subprocess, "run", lambda *a, **k: Architectures())
    monkeypatch.setattr(install_node, "run", fake_run)

    with caplog.at_level("WARNING"):
        assert install_node.install_steam() is False

    assert install_node.STEAM_PACKAGE in caplog.text


def test_sunshine_install_adds_the_cloudsmith_repo_then_the_package(monkeypatch):
    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    calls = []
    monkeypatch.setattr(install_node.shutil, "which", lambda name: None)
    monkeypatch.setattr(install_node, "gaming_user", lambda: None)
    monkeypatch.setattr(install_node, "run", lambda argv, **k: calls.append(argv) or Ok())

    assert install_node.install_sunshine() is True
    assert calls[0] == [
        "sh",
        "-c",
        f"curl -1sLf {install_node.SUNSHINE_CLOUDSMITH_SETUP} | sudo -E bash",
    ]
    assert calls[1] == ["apt-get", "install", "-y", "sunshine"]


def test_a_raising_sunshine_repo_add_is_a_warning_not_a_crash(monkeypatch, caplog):
    def fake_run(argv, **kwargs):
        raise InstallError("could not resolve host")

    monkeypatch.setattr(install_node.shutil, "which", lambda name: None)
    monkeypatch.setattr(install_node, "run", fake_run)

    with caplog.at_level("WARNING"):
        assert install_node.install_sunshine() is False

    assert "LizardByte" in caplog.text


def test_sunshine_already_installed_skips_the_download(monkeypatch):
    monkeypatch.setattr(install_node.shutil, "which", lambda name: "/usr/bin/sunshine")
    monkeypatch.setattr(
        install_node, "run", lambda *a, **k: pytest.fail("should not download anything")
    )
    monkeypatch.setattr(install_node, "gaming_user", lambda: None)

    assert install_node.install_sunshine() is True


def test_sunshine_install_grants_only_the_groups_not_already_held(monkeypatch):
    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    granted = []
    monkeypatch.setattr(install_node.shutil, "which", lambda name: "/usr/bin/sunshine")
    monkeypatch.setattr(install_node, "gaming_user", lambda: "ian")
    monkeypatch.setattr(install_node, "_in_group", lambda player, group: group == "video")
    monkeypatch.setattr(install_node, "run", lambda argv, **k: granted.append(argv) or Ok())

    assert install_node.install_sunshine() is True
    assert ["usermod", "-aG", "input", "ian"] in granted
    assert ["usermod", "-aG", "video", "ian"] not in granted


def test_no_gaming_user_skips_granting_groups(monkeypatch, caplog):
    monkeypatch.setattr(install_node.shutil, "which", lambda name: "/usr/bin/sunshine")
    monkeypatch.setattr(install_node, "gaming_user", lambda: None)
    monkeypatch.setattr(
        install_node, "run", lambda *a, **k: pytest.fail("no account to grant groups to")
    )

    with caplog.at_level("WARNING"):
        assert install_node.install_sunshine() is True

    assert "no gaming user recorded" in caplog.text


def test_no_gaming_user_skips_configuring_the_prep_command(monkeypatch):
    monkeypatch.setattr(install_node, "gaming_user", lambda: None)
    assert install_node.configure_sunshine_prep_command() is False


def test_the_prep_command_starts_and_stops_the_switch(monkeypatch, tmp_path):
    """Wired to the same two commands the polkit rule already lets the gaming
    user run without sudo — hence `elevated: False`, not a password Sunshine
    has no way to supply."""
    real = pwd.getpwuid(os.getuid())
    # A fake account pointing at a throwaway home, so this writes into
    # tmp_path rather than the real account's ~/.config.
    fake_account = pwd.struct_passwd(
        (
            real.pw_name,
            real.pw_passwd,
            real.pw_uid,
            real.pw_gid,
            real.pw_gecos,
            str(tmp_path),
            real.pw_shell,
        )
    )
    restarted = []
    monkeypatch.setattr(install_node, "gaming_user", lambda: real.pw_name)
    monkeypatch.setattr(install_node.pwd, "getpwnam", lambda name: fake_account)
    monkeypatch.setattr(install_node.os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(
        install_node, "run_as_account", lambda argv, *a, **k: restarted.append(argv)
    )

    assert install_node.configure_sunshine_prep_command() is True

    written = json.loads((tmp_path / ".config" / "sunshine" / "apps.json").read_text())
    [prep] = written["global_prep_cmd"]
    assert prep["do"] == f"systemctl start {install_node.gaming_unit_name()}"
    assert prep["undo"] == f"systemctl stop {install_node.gaming_unit_name()}"
    assert prep["elevated"] is False
    assert restarted == [["systemctl", "--user", "restart", "sunshine"]]


def test_an_unchanged_prep_command_does_not_restart_sunshine(monkeypatch, tmp_path):
    real = pwd.getpwuid(os.getuid())
    fake_account = pwd.struct_passwd(
        (
            real.pw_name,
            real.pw_passwd,
            real.pw_uid,
            real.pw_gid,
            real.pw_gecos,
            str(tmp_path),
            real.pw_shell,
        )
    )
    monkeypatch.setattr(install_node, "gaming_user", lambda: real.pw_name)
    monkeypatch.setattr(install_node.pwd, "getpwnam", lambda name: fake_account)
    monkeypatch.setattr(install_node.os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(
        install_node, "run_as_account", lambda *a, **k: pytest.fail("nothing changed to restart")
    )

    # Written once already, byte-for-byte what this call would produce.
    config_dir = tmp_path / ".config" / "sunshine"
    config_dir.mkdir(parents=True)
    rendered = (
        json.dumps(install_node._sunshine_apps_json(install_node.gaming_unit_name()), indent=4)
        + "\n"
    )
    (config_dir / "apps.json").write_text(rendered)

    assert install_node.configure_sunshine_prep_command() is True


def test_install_gaming_runs_every_step_in_order(monkeypatch):
    """The switch's authorisation, a render surface, Steam, Sunshine, then
    wiring Sunshine into the switch — in that order, because Sunshine's config
    names a unit that has to already be installable by the time it is written."""
    order = []
    monkeypatch.setattr(install_node, "systemd_is_running", lambda: True)
    monkeypatch.setattr(install_node, "install_gaming_rule", lambda: order.append("rule"))
    monkeypatch.setattr(install_node.render, "install", lambda: order.append("render") or True)
    monkeypatch.setattr(install_node, "install_steam", lambda: order.append("steam") or True)
    monkeypatch.setattr(install_node, "install_sunshine", lambda: order.append("sunshine") or True)
    monkeypatch.setattr(
        install_node,
        "configure_sunshine_prep_command",
        lambda: order.append("prep-cmd") or True,
    )

    assert install_node.install_gaming() is True
    assert order == ["rule", "render", "steam", "sunshine", "prep-cmd"]
