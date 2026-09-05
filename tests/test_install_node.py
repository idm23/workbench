"""The node installer's own decisions, without installing anything.

Everything here is a step that talks to the machine — a driver probe, a
package installer, a systemd drop-in — so what can be tested is the shape of
what it would do and, more usefully, what it does when the machine cannot
oblige. The container the fresh-install test runs in is exactly that case, and
so is a laptop with no GPU: both have to finish the install and say what is
missing rather than fail.
"""

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
