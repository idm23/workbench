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


def _subprocess_stub(architectures="i386\n", driver_version="595.91.07\n", driver_returncode=0):
    """A fake `subprocess.run` that answers both calls `install_steam()` makes
    directly: the architecture probe and (inside `_nvidia_i386_gl_package()`)
    the driver-version query. Distinguished by argv, the same way the real
    two commands are distinguished by a real machine."""

    class Result:
        def __init__(self, stdout, returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def fake_run(argv, **kwargs):
        if argv[0] == "nvidia-smi":
            return Result(driver_version, driver_returncode)
        return Result(architectures)

    return fake_run


def test_steam_install_installs_the_i386_nvidia_libs_when_there_is_a_gpu(monkeypatch):
    """Found the hard way: without this, Steam's postinst blocks on an
    interactive debconf prompt asking for exactly this package."""

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    calls = []
    monkeypatch.setattr(
        install_node.shutil,
        "which",
        lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None,
    )
    monkeypatch.setattr(install_node.subprocess, "run", _subprocess_stub())
    monkeypatch.setattr(install_node, "run", lambda argv, **k: calls.append(argv) or Ok())

    assert install_node.install_steam() is True
    assert ["apt-get", "install", "-y", "libnvidia-gl-595:i386"] in calls
    # Before Steam itself, so Steam's own postinst never gets a chance to ask.
    assert calls.index(["apt-get", "install", "-y", "libnvidia-gl-595:i386"]) < calls.index(
        ["apt-get", "install", "-y", install_node.STEAM_PACKAGE]
    )


def test_steam_install_skips_the_nvidia_libs_with_no_gpu(monkeypatch):
    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    calls = []
    monkeypatch.setattr(install_node.shutil, "which", lambda name: None)
    monkeypatch.setattr(install_node.subprocess, "run", _subprocess_stub())
    monkeypatch.setattr(install_node, "run", lambda argv, **k: calls.append(argv) or Ok())

    install_node.install_steam()

    assert not any(argv[0] == "apt-get" and "libnvidia-gl" in argv[-1] for argv in calls)


def test_a_failed_nvidia_i386_install_does_not_block_steam(monkeypatch, caplog):
    """The exact package name is a property of the driver series, which
    changes — a wrong guess must not cost the rest of the install."""

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        if argv == ["apt-get", "install", "-y", "libnvidia-gl-595:i386"]:
            raise InstallError("no such package")
        return Ok()

    monkeypatch.setattr(
        install_node.shutil,
        "which",
        lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None,
    )
    monkeypatch.setattr(install_node.subprocess, "run", _subprocess_stub())
    monkeypatch.setattr(install_node, "run", fake_run)

    with caplog.at_level("WARNING"):
        assert install_node.install_steam() is True

    assert "libnvidia-gl-595:i386" in caplog.text


def test_nvidia_i386_package_tracks_the_installed_driver_series(monkeypatch):
    """`595.91.07` -> `595` -> `libnvidia-gl-595:i386` — matching the same
    series already installed in 64-bit, not a name fixed at write time."""
    monkeypatch.setattr(
        install_node.subprocess, "run", _subprocess_stub(driver_version="610.14.02\n")
    )
    assert install_node._nvidia_i386_gl_package() == "libnvidia-gl-610:i386"


def test_no_driver_version_reported_skips_cleanly(monkeypatch):
    monkeypatch.setattr(install_node.subprocess, "run", _subprocess_stub(driver_returncode=1))
    assert install_node._nvidia_i386_gl_package() is None


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
    restarted = []
    monkeypatch.setattr(install_node.shutil, "which", lambda name: "/usr/bin/sunshine")
    monkeypatch.setattr(install_node, "gaming_user", lambda: "ian")
    monkeypatch.setattr(install_node, "_in_group", lambda player, group: group == "video")
    monkeypatch.setattr(install_node, "run", lambda argv, **k: granted.append(argv) or Ok())
    monkeypatch.setattr(install_node.pwd, "getpwnam", lambda name: pwd.getpwuid(os.getuid()))
    monkeypatch.setattr(
        install_node, "restart_user_manager", lambda account: restarted.append(account)
    )

    assert install_node.install_sunshine() is True
    assert ["usermod", "-aG", "input", "ian"] in granted
    assert ["usermod", "-aG", "video", "ian"] not in granted
    # A group was actually granted, so the account's already-running user
    # manager (from an earlier `loginctl enable-linger`) needs a restart to
    # ever see it — found the hard way, when it silently did not.
    assert len(restarted) == 1


def test_sunshine_install_skips_the_restart_when_nothing_changed(monkeypatch):
    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(install_node.shutil, "which", lambda name: "/usr/bin/sunshine")
    monkeypatch.setattr(install_node, "gaming_user", lambda: "ian")
    monkeypatch.setattr(install_node, "_in_group", lambda player, group: True)
    monkeypatch.setattr(install_node, "run", lambda argv, **k: Ok())
    monkeypatch.setattr(
        install_node,
        "restart_user_manager",
        lambda account: pytest.fail("nothing changed to restart"),
    )

    assert install_node.install_sunshine() is True


def test_no_gaming_user_skips_granting_groups(monkeypatch, caplog):
    monkeypatch.setattr(install_node.shutil, "which", lambda name: "/usr/bin/sunshine")
    monkeypatch.setattr(install_node, "gaming_user", lambda: None)
    monkeypatch.setattr(
        install_node, "run", lambda *a, **k: pytest.fail("no account to grant groups to")
    )

    with caplog.at_level("WARNING"):
        assert install_node.install_sunshine() is True

    assert "no gaming user recorded" in caplog.text


def test_linger_is_only_enabled_once(monkeypatch):
    calls = []
    monkeypatch.setattr(install_node, "_linger_enabled", lambda player: False)
    monkeypatch.setattr(install_node, "run", lambda argv, **k: calls.append(argv))

    install_node._ensure_linger("ian")

    assert calls == [["loginctl", "enable-linger", "ian"]]


def test_linger_already_enabled_is_left_alone(monkeypatch):
    monkeypatch.setattr(install_node, "_linger_enabled", lambda player: True)
    monkeypatch.setattr(
        install_node,
        "run",
        lambda *a, **k: pytest.fail("enable-linger should not have been called"),
    )

    install_node._ensure_linger("ian")  # must not raise


def test_a_failed_sunshine_enable_is_a_warning_not_a_crash(monkeypatch, tmp_path, caplog):
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

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "Unit app-dev.lizardbyte.app.Sunshine.service not found."

    monkeypatch.setattr(install_node, "gaming_user", lambda: real.pw_name)
    monkeypatch.setattr(install_node.pwd, "getpwnam", lambda name: fake_account)
    monkeypatch.setattr(install_node.os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(install_node, "_ensure_linger", lambda player: None)
    monkeypatch.setattr(install_node, "run_as_account", lambda *a, **k: Failed())

    with caplog.at_level("WARNING"):
        assert install_node.configure_sunshine_prep_command() is False

    assert install_node.SUNSHINE_UNIT_NAME in caplog.text


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

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    enabled = []
    monkeypatch.setattr(install_node, "gaming_user", lambda: real.pw_name)
    monkeypatch.setattr(install_node.pwd, "getpwnam", lambda name: fake_account)
    monkeypatch.setattr(install_node.os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(install_node, "_ensure_linger", lambda player: None)
    monkeypatch.setattr(
        install_node, "run_as_account", lambda argv, *a, **k: enabled.append(argv) or Ok()
    )

    assert install_node.configure_sunshine_prep_command() is True

    written = json.loads((tmp_path / ".config" / "sunshine" / "apps.json").read_text())
    [app] = written["apps"]
    assert app["name"] == "Desktop"

    # The prep commands belong in sunshine.conf, NOT in apps.json: this
    # version of Sunshine reads them from there and silently ignores the key
    # in apps.json, which is how the switch spent its whole life never firing
    # while every stream looked perfect. Asserted from both sides.
    assert "global_prep_cmd" not in written
    conf = (tmp_path / ".config" / "sunshine" / "sunshine.conf").read_text()
    [line] = [n for n in conf.splitlines() if n.startswith("global_prep_cmd")]
    [prep] = json.loads(line.split("=", 1)[1].strip())
    assert prep["do"] == f"systemctl start {install_node.gaming_unit_name()}"
    assert prep["undo"] == f"systemctl stop {install_node.gaming_unit_name()}"
    # `elevated` is a Windows-only field in Sunshine's own UI; on Linux the
    # authorisation is the polkit rule.
    assert "elevated" not in prep

    assert ["systemctl", "--user", "enable", "--now", install_node.SUNSHINE_UNIT_NAME] in enabled


def test_an_unchanged_prep_command_still_ensures_sunshine_is_enabled(monkeypatch, tmp_path):
    """Found the hard way: skipping `enable --now` on an unchanged file left
    Sunshine enabled-but-stopped whenever something else in the same install
    (a group grant's `restart_user_manager`) stopped it in between. The
    config file's idempotency and the unit's running state are two different
    questions, and only the first one used to gate this call."""
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

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    enabled = []
    monkeypatch.setattr(install_node, "gaming_user", lambda: real.pw_name)
    monkeypatch.setattr(install_node.pwd, "getpwnam", lambda name: fake_account)
    monkeypatch.setattr(install_node.os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(install_node, "_ensure_linger", lambda player: None)
    monkeypatch.setattr(
        install_node, "run_as_account", lambda argv, *a, **k: enabled.append(argv) or Ok()
    )

    # Written once already, byte-for-byte what this call would produce.
    config_dir = tmp_path / ".config" / "sunshine"
    config_dir.mkdir(parents=True)
    (config_dir / "apps.json").write_text(
        json.dumps(install_node._sunshine_apps_json(), indent=4) + "\n"
    )
    (config_dir / "sunshine.conf").write_text(
        install_node._sunshine_conf_with_prep("", install_node.gaming_unit_name())
    )

    assert install_node.configure_sunshine_prep_command() is True
    assert ["systemctl", "--user", "enable", "--now", install_node.SUNSHINE_UNIT_NAME] in enabled


def test_sunshine_is_wanted_by_a_target_a_headless_node_actually_reaches(monkeypatch, tmp_path):
    """The bug this pins down was invisible until a reboot.

    Sunshine ships `WantedBy=graphical-session.target`. A gaming node never
    reaches that target — its X server is a system unit and nobody logs in —
    so `systemctl --user enable` linked it somewhere that is never activated,
    reported `enabled`, and the unit could not start at boot. `enable --now`
    hid it completely: the `--now` half started Sunshine during the install,
    so it was always streaming by the time anyone looked.
    """
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

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    calls = []
    monkeypatch.setattr(install_node.os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(
        install_node, "run_as_account", lambda argv, *a, **k: calls.append(argv) or Ok()
    )

    assert install_node._ensure_headless_autostart(fake_account) is True

    assert [
        "systemctl",
        "--user",
        "add-wants",
        "default.target",
        install_node.SUNSHINE_UNIT_NAME,
    ] in calls

    dropin = tmp_path / install_node.HEADLESS_DROPIN
    body = dropin.read_text()
    # Clearing After= drops the ordering against a target this machine never
    # reaches; the ExecStartPre replaces Sunshine's packaged `sleep 5` guess
    # with a wait for the display it actually needs.
    assert "After=\n" in body
    assert "/tmp/.X11-unix/X0" in body


def test_the_autostart_link_is_made_even_when_the_dropin_is_unchanged(monkeypatch, tmp_path):
    """Same lesson as `enable --now`, one level up: the drop-in's content and
    whether the symlink exists are two different questions, and only the
    second one keeps a node streaming after a reboot."""
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

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    calls = []
    monkeypatch.setattr(install_node.os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(
        install_node, "run_as_account", lambda argv, *a, **k: calls.append(argv) or Ok()
    )

    dropin = tmp_path / install_node.HEADLESS_DROPIN
    dropin.parent.mkdir(parents=True)
    dropin.write_text(install_node.HEADLESS_DROPIN_BODY)

    assert install_node._ensure_headless_autostart(fake_account) is False
    assert [
        "systemctl",
        "--user",
        "add-wants",
        "default.target",
        install_node.SUNSHINE_UNIT_NAME,
    ] in calls


def test_an_existing_sunshine_conf_keeps_its_other_settings(monkeypatch):
    """Sunshine rewrites this whole file itself whenever anyone saves from its
    web UI, so this has to converge rather than stack up a duplicate key per
    install — and it must not eat the settings a person put there."""
    existing = "csrf_allowed_origins = https://node:47990\nmin_log_level = 2\n"

    once = install_node._sunshine_conf_with_prep(existing, "workbench-gaming.service")
    assert "csrf_allowed_origins = https://node:47990" in once
    assert "min_log_level = 2" in once
    assert once.count("global_prep_cmd") == 1

    # Run again over its own output: same file, not a second key.
    twice = install_node._sunshine_conf_with_prep(once, "workbench-gaming.service")
    assert twice == once

    # And a stale command is replaced rather than appended beside.
    moved = install_node._sunshine_conf_with_prep(once, "some-other.service")
    assert moved.count("global_prep_cmd") == 1
    assert "some-other.service" in moved
    assert "workbench-gaming.service" not in moved


def test_a_client_node_does_not_claim_to_serve_a_model(monkeypatch, caplog):
    """It used to. A client node finished its install announcing that it was
    "serving qwen3:8b" at a loopback endpoint nothing listened on, and told the
    reader to point a head at it. Every word false, printed in bold, by an
    install that had otherwise succeeded."""
    monkeypatch.setattr(install_node, "is_inference_node", lambda: False)
    monkeypatch.setattr(install_node, "is_gaming_node", lambda: False)
    monkeypatch.setattr(install_node, "is_client_node", lambda: True)
    monkeypatch.setattr(install_node, "stream_host", lambda: "192.168.1.155")

    with caplog.at_level("INFO"):
        install_node.report_success()

    assert "serving" not in caplog.text
    assert "11434" not in caplog.text
    assert "ollama" not in caplog.text
    # It should say what it actually does, and what is left.
    assert "streaming to a screen" in caplog.text
    assert "192.168.1.155" in caplog.text


def test_an_inference_node_still_gets_its_endpoint_and_commands(monkeypatch, caplog):
    monkeypatch.setattr(install_node, "is_inference_node", lambda: True)
    monkeypatch.setattr(install_node, "is_gaming_node", lambda: False)
    monkeypatch.setattr(install_node, "is_client_node", lambda: False)

    with caplog.at_level("INFO"):
        install_node.report_success()

    assert "11434" in caplog.text
    assert "ollama" in caplog.text


def test_the_client_unit_renders_without_a_compositor(monkeypatch):
    """The client must be drivable from a script. moonlight-qt is not: it
    ignores --video-decoder, ignores videodecoderselection in its own config
    file, and shows its pairing PIN only in a GUI dialog - unobtainable over
    SSH on a machine with no desktop. Moonlight Embedded is CLI-native, which
    is why the unit calls `moonlight` and sets no window-system hints at all."""
    real = pwd.getpwuid(os.getuid())

    unit = install_node._client_unit("192.168.1.155", real)

    assert "/usr/local/bin/moonlight stream 192.168.1.155" in unit
    assert "-app Desktop" in unit
    # No Qt platform, no SDL video driver: there is no window system to hint at.
    assert "SDL_VIDEODRIVER" not in unit
    assert "moonlight-qt" not in unit
    # Audio needs the runtime dir, or the stream plays video in silence.
    assert "XDG_RUNTIME_DIR" in unit
    assert f"User={real.pw_name}" in unit
    # It must come back by itself: nobody is looking at this machine.
    assert "Restart=on-failure" in unit
    assert "WantedBy=multi-user.target" in unit


def test_the_client_grants_journal_access_so_its_doctor_check_can_answer():
    """Without `systemd-journal` the service account cannot open the unit's
    journal, so the decoder check reports `unknown` forever whatever the
    machine is doing - a check occupying the space where an answer would go."""
    assert "systemd-journal" in install_node.CLIENT_GROUPS


def test_a_built_client_is_not_rebuilt(monkeypatch):
    """Idempotent on the only check that means anything: does the binary run."""
    monkeypatch.setattr(install_node.shutil, "which", lambda name: "/usr/local/bin/moonlight")
    monkeypatch.setattr(
        install_node, "run_as_account", lambda *a, **k: pytest.fail("nothing to rebuild")
    )

    assert install_node.build_moonlight(pwd.getpwuid(os.getuid())) is True


def test_client_audio_is_pushed_out_of_hdmi(monkeypatch):
    """PipeWire's default on a Pi is the 3.5mm jack, which on a machine wired to
    a television by one HDMI cable is always wrong - and silently so: the
    stream carries perfect audio to a socket with nothing in it. Hit twice on
    real hardware, once per Pi, because nothing anywhere reports it."""
    real = pwd.getpwuid(os.getuid())

    class Listing:
        returncode = 0
        stderr = ""
        stdout = (
            "  Sinks:\n"
            "   *   68. Built-in Audio Stereo               [vol: 0.40]\n"
            "       69. Built-in Audio Digital Stereo (HDMI) [vol: 0.40]\n"
        )

    calls = []
    monkeypatch.setattr(
        install_node, "run_as_account", lambda argv, *a, **k: calls.append(argv) or Listing()
    )

    assert install_node.prefer_hdmi_audio(real) is True
    assert ["wpctl", "set-default", "69"] in calls
    # 69, not 68: the analogue jack is the one it must not pick.
    assert ["wpctl", "set-default", "68"] not in calls


def test_no_hdmi_sink_is_a_warning_not_a_crash(monkeypatch, caplog):
    real = pwd.getpwuid(os.getuid())

    class Listing:
        returncode = 0
        stderr = ""
        stdout = "   *   68. Built-in Audio Stereo   [vol: 0.40]\n"

    monkeypatch.setattr(install_node, "run_as_account", lambda *a, **k: Listing())

    with caplog.at_level("WARNING"):
        assert install_node.prefer_hdmi_audio(real) is False

    assert "silent" in caplog.text


def test_the_client_unit_grants_the_groups_the_display_needs(monkeypatch):
    """`video`/`render` open the DRM devices, `input` is the virtual gamepad,
    `audio` is the HDMI sink. A missing group here is invisible until the one
    path that needed it is the one that breaks."""
    real = pwd.getpwuid(os.getuid())

    unit = install_node._client_unit("node", real)

    for group in ("video", "render", "input", "audio"):
        assert group in unit


def test_the_stream_host_argument_is_parsed_in_both_spellings(monkeypatch):
    for argv in (["--stream-host", "node-1"], ["--stream-host=node-1"]):
        monkeypatch.setattr(install_node.sys, "argv", ["install_node.py", *argv])
        assert install_node._stream_host_argument() == "node-1"


def test_saying_nothing_about_the_stream_host_leaves_it_alone(monkeypatch):
    """Same rule as `--head`: a re-install that does not mention it must leave
    a client streaming from exactly what it streamed from before."""
    monkeypatch.setattr(install_node.sys, "argv", ["install_node.py", "--role=node"])
    assert install_node._stream_host_argument() is None


def test_a_client_with_no_stream_host_writes_no_unit(monkeypatch, caplog):
    """Refused rather than guessed. A unit pointed at nothing would restart
    every five seconds forever, which is a worse way to learn the same thing."""
    monkeypatch.setattr(install_node, "systemd_is_running", lambda: True)
    monkeypatch.setattr(install_node, "_package_installed", lambda name: True)
    monkeypatch.setattr(install_node, "_service_passwd", lambda: pwd.getpwuid(os.getuid()))
    monkeypatch.setattr(install_node, "_in_group", lambda user, group: True)
    monkeypatch.setattr(install_node, "_ensure_linger", lambda player: None)
    monkeypatch.setattr(install_node, "stream_host", lambda: None)
    monkeypatch.setattr(install_node, "build_moonlight", lambda account: True)
    monkeypatch.setattr(install_node, "prefer_hdmi_audio", lambda account: True)

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(install_node, "run_as_account", lambda *a, **k: Ok())
    written = []
    monkeypatch.setattr(install_node, "write_privileged", lambda *a, **k: written.append(a))

    with caplog.at_level("WARNING"):
        assert install_node.install_client() is False

    assert written == []
    assert "stream host" in caplog.text


def test_install_gaming_runs_every_step_in_order(monkeypatch):
    """The switch's authorisation, a render surface, Steam, Sunshine, audio,
    then wiring Sunshine into the switch — in that order, because Sunshine's
    config names a unit that has to already be installable by the time it is
    written, and because Sunshine picks its capture device at startup, so the
    step that leaves it running has to come after the one that creates a sink
    for it to find."""
    order = []
    monkeypatch.setattr(install_node, "systemd_is_running", lambda: True)
    monkeypatch.setattr(install_node, "install_gaming_rule", lambda: order.append("rule"))
    monkeypatch.setattr(install_node.render, "install", lambda: order.append("render") or True)
    monkeypatch.setattr(install_node, "install_steam", lambda: order.append("steam") or True)
    monkeypatch.setattr(install_node, "install_sunshine", lambda: order.append("sunshine") or True)
    monkeypatch.setattr(install_node, "install_audio", lambda: order.append("audio") or True)
    monkeypatch.setattr(
        install_node,
        "configure_sunshine_prep_command",
        lambda: order.append("prep-cmd") or True,
    )

    assert install_node.install_gaming() is True
    assert order == ["rule", "render", "steam", "sunshine", "audio", "prep-cmd"]
