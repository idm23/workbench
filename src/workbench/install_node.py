"""Install a worker node: a machine that lends the head something it lacks.

Today that is a GPU serving an OpenAI-compatible endpoint, which
`workbench.agents.local` then drives. A node runs no web app, holds no
database, and executes no runs — it answers `/v1/chat/completions` and keeps
itself up to date, and that is the whole of it.

The Jake test extends rather than bends: a fresh Ubuntu machine, a clone, and
`./install.sh --role=node` produces a node. What it cannot automate it names,
exactly as the head's install does — a driver that needs a reboot and a head
address nobody can guess are steps a person takes, not steps that go missing.

Two things here are deliberately *not* ours to own:

- **The GPU driver is reported, never installed.** A driver install is
  reboot-shaped, and an unattended script that reboots a machine someone is
  standing at is worse than one that tells them what to run.
- **The model server is Ollama, driven rather than reimplemented.** We install
  it, own a drop-in that decides where it listens, and pull the model. Its
  CUDA handling is the part we least want to maintain, and `llama-server` or
  vLLM fit behind the same URL if it ever disappoints.
"""

import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from workbench.config import (
    ROLE_NODE,
    deploy_branch,
    inference_base_url,
    local_model,
    repo_root,
)
from workbench.install import (
    DEPLOY_INTERVAL,
    InstallError,
    _service_passwd,
    _venv_bin,
    become_root,
    build_environment,
    check_invocation,
    check_prerequisites,
    enable_deploy_timer,
    ensure_data_directory,
    ensure_service_account,
    ensure_uv_for_owner,
    hand_off_to,
    info,
    install_units,
    needs_relocation,
    record_role,
    relocate,
    report_outstanding,
    run,
    step,
    systemd_is_running,
    warn,
    write_privileged,
)
from workbench.logs import BOLD, RED, configure_console_logging, paint

logger = logging.getLogger(__name__)

ENTRY = "workbench.install_node"

#: Ollama's own installer. A `curl | sh` inside our installer is not something
#: to do lightly, and it is here because the alternative is worse: the vendor
#: ships CUDA libraries matched to their runtime, and a hand-rolled unpack that
#: gets that pairing wrong fails as "no GPU found" on a machine with a GPU.
OLLAMA_INSTALLER = "https://ollama.com/install.sh"

#: The drop-in we own on top of the unit Ollama installs. Its own unit is
#: theirs and gets replaced on upgrade; a drop-in survives that, which is the
#: difference between configuration that holds and configuration that lasts
#: until the next `ollama` release.
OLLAMA_DROP_IN_DIR = "/etc/systemd/system/ollama.service.d"
OLLAMA_DROP_IN = "workbench.conf"

#: Where the model server listens. Every interface, and this is the one
#: decision on a node worth arguing about — see `_drop_in` below.
OLLAMA_BIND = "0.0.0.0:11434"

#: How long a model may stay resident with nothing asking for it. Long enough
#: that a plan run followed by an execute run does not pay to load 5 GB twice;
#: short enough that a laptop is not pinning its VRAM overnight.
OLLAMA_KEEP_ALIVE = "30m"

#: How long to wait for the server to answer once it has been started.
ENDPOINT_TIMEOUT_SECONDS = 60.0


def check_gpu() -> None:
    """Say what the GPU situation is, and never fail on it.

    A node with no usable GPU still works — it is simply slow, because the
    model runs on CPU — so this is information rather than a gate. What it must
    not do is stay quiet: "why is every run taking twenty minutes" is not a
    question anyone should have to answer twice.
    """
    if shutil.which("nvidia-smi") is None:
        warn("no nvidia-smi here, so the model will run on the CPU.")
        info("For an NVIDIA card:  sudo ubuntu-drivers install")
        info("Then reboot, and re-run this installer.")
        return

    probe = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        warn("nvidia-smi is installed but did not answer; the driver may need a reboot.")
        return
    for line in probe.stdout.strip().splitlines():
        info(f"GPU: {line.strip()}")


def _drop_in() -> str:
    """The systemd drop-in that decides where the model server listens.

    `0.0.0.0`, which is a decision rather than a default. A head reaches its
    node over the LAN — that is the fast path and, on this pair of machines,
    currently the only one — while the tailnet is the fallback, and
    `OLLAMA_HOST` takes exactly one address. Binding both means binding all.

    The consequence is real and belongs in the open: the endpoint is
    unauthenticated, so anything on the home network can spend this GPU. That
    is a wider audience than the tailnet the app's own "no auth at the app
    layer" decision was written against, and the mitigation if it ever matters
    is a firewall rule here rather than a setting in Workbench.
    """
    return (
        "# Written by Workbench's node installer. Edits here are overwritten;\n"
        "# change WORKBENCH_INFERENCE_URL or this file's template instead.\n"
        "[Service]\n"
        f"Environment=OLLAMA_HOST={OLLAMA_BIND}\n"
        f"Environment=OLLAMA_KEEP_ALIVE={OLLAMA_KEEP_ALIVE}\n"
    )


def install_inference_server() -> bool:
    """Put Ollama on the machine and make it listen where the head can reach it.

    Returns whether there is a server to talk to at all. False is an ordinary
    answer, not a failure: a container has no systemd, so there is nothing to
    manage and nothing to start, and the install still has to finish and say so
    — that path is what the fresh-install test exercises.
    """
    if not systemd_is_running():
        warn("systemd is not running here (normal inside a container).")
        info("Skipping the model server. On a real node this step installs Ollama,")
        info(f"binds it to {OLLAMA_BIND}, and pulls {local_model()}.")
        return False

    if shutil.which("ollama") is None:
        info("installing Ollama")
        # Streamed: it is a large download, and a silent installer looks hung.
        result = run(["sh", "-c", f"curl -fsSL {OLLAMA_INSTALLER} | sh"], stream=True)
        if result.returncode != 0:
            raise InstallError(
                "Ollama's installer failed. Install it by hand and re-run this:\n"
                f"       curl -fsSL {OLLAMA_INSTALLER} | sh"
            )
    else:
        info(f"Ollama already installed at {shutil.which('ollama')}")

    directory = Path(OLLAMA_DROP_IN_DIR)
    target = directory / OLLAMA_DROP_IN
    rendered = _drop_in()
    if not (target.is_file() and target.read_text() == rendered):
        run(["mkdir", "-p", OLLAMA_DROP_IN_DIR], privileged=True)
        write_privileged(target, rendered, staged_as=OLLAMA_DROP_IN)
        run(["systemctl", "daemon-reload"], privileged=True)
        run(["systemctl", "restart", "ollama"], privileged=True)
        info(f"wrote {target} and restarted ollama")
    else:
        info("model server configuration already up to date")

    run(["systemctl", "enable", "--quiet", "ollama"], privileged=True)
    return True


def pull_model() -> None:
    """Fetch the model this node is meant to serve.

    Gigabytes, once, streamed so the progress is visible. Idempotent: Ollama
    re-uses what it already has, so re-running the installer costs a manifest
    check rather than another download.
    """
    model = local_model()
    step(f"Pulling {model}")
    result = run(["ollama", "pull", model], stream=True)
    if result.returncode != 0:
        # Not fatal. The node is otherwise installed and the fix is one
        # command, which is better said here than by a run failing later.
        warn(f"could not pull {model}. The node is installed; pull it with:")
        info(f"    ollama pull {model}")
        return
    info(f"{model} is available on this node")


def wait_for_endpoint() -> None:
    """Wait until the server answers, so an install that says it worked did."""
    url = f"{inference_base_url()}/models"
    deadline = time.monotonic() + ENDPOINT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            # urllib rather than httpx, like the head's health check: this
            # module has to stay importable before a virtualenv exists.
            with urllib.request.urlopen(url, timeout=2.0) as answer:
                if answer.status == 200:
                    info(f"the model server answers at {url}")
                    return
        except urllib.error.URLError, OSError:
            pass
        time.sleep(0.5)

    warn(f"the model server did not answer {url} within {ENDPOINT_TIMEOUT_SECONDS:.0f}s.")
    info("Check:  systemctl status ollama")


def report_success() -> None:
    """What this node now is, and the one thing a person still has to do."""
    logger.info("\n%s", paint(BOLD, f"This machine is a Workbench node, serving {local_model()}"))
    logger.info(
        "%s",
        f"""
It serves {inference_base_url()} and updates itself from '{deploy_branch()}'
every {DEPLOY_INTERVAL}. Nothing needs running here again.

Point a head at it — on the head, in /etc/workbench/env:

    WORKBENCH_AGENT_BACKEND=local
    WORKBENCH_INFERENCE_URL=http://{_lan_address() or "this-machine"}:11434/v1

Useful commands:

    {_venv_bin("python")} -m workbench.doctor   # re-check everything below

    systemctl status ollama
    journalctl -u ollama -f
    ollama ps                                    # what is loaded right now
""",
    )


def _lan_address() -> str | None:
    """This machine's address on the local network, for the line above.

    The LAN first, because that is the path the head should prefer: one hop
    between two machines in the same house, where the tailnet address is a
    working fallback rather than the route to take by default. Best effort — a
    printed hint that is occasionally blank is fine, where a wrong address
    confidently printed is not.
    """
    probe = subprocess.run(["hostname", "-I"], capture_output=True, text=True, check=False)
    for candidate in probe.stdout.split():
        if candidate.startswith("100.") or ":" in candidate:
            # Skip the tailnet address and IPv6: this line is the LAN hint.
            continue
        return candidate
    return None


def main() -> int:
    configure_console_logging()
    os.chdir(repo_root())

    try:
        check_invocation()

        step("Checking prerequisites")
        check_prerequisites()
        check_gpu()

        become_root(ENTRY)

        if needs_relocation():
            step("Preparing the service account")
            account = ensure_service_account()

            step(f"Moving the deployment to {repo_root()}")
            hand_off_to(relocate(account), ENTRY)
            return 0  # unreachable: hand_off_to execs

        account = _service_passwd()
        info(f"deployment at {repo_root()}, owned by '{account.pw_name}'")

        step("Building the environment")
        build_environment(ensure_uv_for_owner(account))

        # No migrations and no agent state: a node holds no database and runs
        # no agent. It serves a model, which is the whole difference.
        step("Recording what this machine is")
        ensure_data_directory(account)
        record_role(ROLE_NODE, account)

        step("Installing the model server")
        serving = install_inference_server()
        if serving:
            pull_model()

        step("Installing the updater")
        if not systemd_is_running():
            logger.info("\n%s\n", paint(BOLD, "Node install complete (without any units)."))
        else:
            install_units()
            enable_deploy_timer()

            if serving:
                step("Waiting for the model server to answer")
                wait_for_endpoint()

            report_success()

        # Said on every path, including the one that installed no units at all.
        # The head's installer learned this the hard way: an early return that
        # skipped it was how a machine could finish an install perfectly and
        # never mention the one step a person still had to take.
        step("Checking what still needs a person")
        report_outstanding()
        return 0

    except InstallError as error:
        logger.error("\n%s %s\n", paint(RED, "error:"), error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
