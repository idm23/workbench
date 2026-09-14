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

import json
import logging
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from workbench import render
from workbench.config import (
    CAPABILITIES,
    INFERENCE,
    ROLE_NODE,
    declared_capabilities,
    deploy_branch,
    gaming_unit_name,
    gaming_user,
    head_url,
    inference_base_url,
    is_client_node,
    is_gaming_node,
    is_inference_node,
    local_model,
    repo_root,
    stream_host,
)
from workbench.install import (
    DEPLOY_INTERVAL,
    SYSTEMD_DIR,
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
    install_gaming_rule,
    install_units,
    needs_relocation,
    record_capabilities,
    record_gaming_user,
    record_head,
    record_role,
    record_stream_host,
    relocate,
    report_outstanding,
    restart_user_manager,
    run,
    run_as_account,
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

#: How long to wait on the head when registering. Short: it is one small POST
#: over a LAN, and a head that is off should cost a warning rather than a wait.
REGISTER_TIMEOUT_SECONDS = 10

#: How to spell the head's address, wherever this has to say so. Not
#: `http://<host>:8787`: the head's app binds loopback and is published by
#: `tailscale serve`, so that form reaches nothing from another machine — and
#: it is exactly what someone writes when guessing.
HEAD_HINT = "--head <the URL you open Workbench on>"

#: What this node advertises when it can serve a model. The string the head
#: matches on — see `workbench.nodes.INFERENCE`, which is the same word from
#: the other side, and `config.INFERENCE`, which is where it actually lives so
#: that the two sides cannot drift.
INFERENCE_CAPABILITY = INFERENCE

#: How long to wait when asking whether this node's own model server is up.
#: Shorter than the head's probe: this is a loopback request, and it is made on
#: every registration, which is every deploy tick.
SERVING_TIMEOUT_SECONDS = 2.0


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


def _endpoint_answers(timeout: float = SERVING_TIMEOUT_SECONDS) -> bool:
    """Whether this node's own model server is answering, right now."""
    try:
        # urllib rather than httpx, like the head's health check: this module
        # has to stay importable before a virtualenv exists.
        with urllib.request.urlopen(f"{inference_base_url()}/models", timeout=timeout) as answer:
            return answer.status == 200
    except urllib.error.URLError, OSError:
        return False


def wait_for_endpoint() -> None:
    """Wait until the server answers, so an install that says it worked did."""
    url = f"{inference_base_url()}/models"
    deadline = time.monotonic() + ENDPOINT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _endpoint_answers():
            info(f"the model server answers at {url}")
            return
        time.sleep(0.5)

    warn(f"the model server did not answer {url} within {ENDPOINT_TIMEOUT_SECONDS:.0f}s.")
    info("Check:  systemctl status ollama")


def report_success() -> None:
    """What this node now is, and the one thing a person still has to do.

    Written per capability rather than as one story, because the one story used
    to be the inference one: a client node finished its install by announcing
    that it was "serving qwen3:8b" at a loopback endpoint nothing listened on,
    and telling the reader to point a head at it. Every word of that was false,
    and the install had just printed it in bold.
    """
    doing = []
    if is_inference_node():
        doing.append(f"serving {local_model()}")
    if is_gaming_node():
        doing.append("lending its GPU to a screen")
    if is_client_node():
        doing.append("streaming to a screen")
    summary = ", ".join(doing) if doing else "connected and updating itself"
    logger.info("\n%s", paint(BOLD, f"This machine is a Workbench node, {summary}"))

    lines = [
        f"\nIt updates itself from '{deploy_branch()}' every {DEPLOY_INTERVAL}.",
    ]

    if is_inference_node():
        lines.append(
            f"""
It serves {inference_base_url()}. Point a head at it — on the head,
in /etc/workbench/env:

    WORKBENCH_AGENT_BACKEND=local
    WORKBENCH_INFERENCE_URL=http://{_lan_address() or "this-machine"}:11434/v1

    systemctl status ollama
    journalctl -u ollama -f
    ollama ps                                    # what is loaded right now"""
        )

    if is_client_node():
        lines.append(
            f"""
It streams from {stream_host() or "nothing yet"}. The unit is installed but not
started: pair this client first, at https://{stream_host() or "<the host>"}:47990.

    systemctl status {CLIENT_UNIT_NAME}
    journalctl -u {CLIENT_UNIT_NAME} -f"""
        )

    lines.append(
        f"""
    {_venv_bin("python")} -m workbench.doctor   # re-check everything below
"""
    )
    logger.info("%s", "\n".join(lines))


#: Addresses to leave out of what a node advertises. Docker's bridge is
#: reachable from nowhere but this machine, and a link-local address is worse
#: than useless to a head: it resolves and then does not work.
_UNROUTABLE_PREFIXES = ("172.17.", "169.254.", "127.")


def addresses() -> list[str]:
    """Every way to reach this node, best route first.

    LAN before tailnet, because one hop between two machines in the same house
    beats WireGuard and a coordination server — and because the head probes
    this list in order rather than trusting it, a route that stops working
    costs one failed connection rather than a run.

    IPv6 is left out. Both paths here are IPv4, and an address that resolves
    but does not route is the failure this list exists to avoid.
    """
    probe = subprocess.run(["hostname", "-I"], capture_output=True, text=True, check=False)
    lan: list[str] = []
    tailnet: list[str] = []
    for candidate in probe.stdout.split():
        if ":" in candidate or candidate.startswith(_UNROUTABLE_PREFIXES):
            continue
        # 100.64.0.0/10 is the shared address space Tailscale hands out.
        (tailnet if candidate.startswith("100.") else lan).append(candidate)
    return lan + tailnet


def _lan_address() -> str | None:
    """The first LAN address, for the hint printed at the end of an install."""
    found = [one for one in addresses() if not one.startswith("100.")]
    return found[0] if found else None


def gpu_description() -> str | None:
    """What this node is lending, in one line, or None if it has no card."""
    if shutil.which("nvidia-smi") is None:
        return None
    probe = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = probe.stdout.strip().splitlines() if probe.returncode == 0 else []
    return lines[0].strip() if lines else None


def capabilities() -> list[str]:
    """What this node is offering the head *right now*.

    The declared list narrowed to what is actually true this second. Two ideas
    meet here and both are load-bearing:

    - **Declared** is what someone installed this machine to do, and it does
      not change on its own. It lives in `data/capabilities`.
    - **Offered** is what it can do at this moment. `inference` is withdrawn
      whenever the model server is not answering — because a game has the card,
      because it crashed, because the disk filled, because it is mid-upgrade.

    Deriving rather than setting is what makes this survive the deploy timer.
    The obvious design — something writes "busy" when a game starts — is a bug
    here, because `deploy.converge_node()` re-registers every five minutes and
    would cheerfully un-announce it again mid-game. Computing the answer from
    state instead turns that tick from a clobber into a repair, which is the
    same rule the units and the restart already follow.

    It also fixes something that was already wrong: a node whose model server
    had died went on advertising `inference`, so every run paid a probe and
    then failed.

    Asking the endpoint rather than `systemctl is-active ollama` is deliberate.
    It is the same question the head asks (`nodes._answers`), it needs no
    systemd — so it answers honestly inside the fresh-install container — and
    it covers every reason the server is down rather than the one we thought of.
    """
    declared = declared_capabilities()
    # Probed only when it was declared, so a node that never served a model
    # does not pay a loopback timeout on every deploy tick.
    serving = INFERENCE in declared and _endpoint_answers()
    return [name for name in declared if name != INFERENCE or serving]


def register_with_head() -> None:
    """Tell the head this node exists, and how to reach it.

    Best effort by design. A head that is off, or on the other side of a
    network that is down, must not fail a node's install — the node still
    serves models, and the next deploy tries again in five minutes. What it
    must not do is stay quiet about having failed.
    """
    head = head_url()
    if head is None:
        info("no head configured, so this node is not registered with one.")
        info(f"Re-run with {HEAD_HINT} to register it.")
        return

    payload = json.dumps(
        {
            "name": socket.gethostname(),
            "addresses": addresses(),
            "capabilities": capabilities(),
            "model": local_model(),
            "gpu": gpu_description(),
        }
    ).encode()
    request = urllib.request.Request(
        f"{head}/api/nodes",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # urllib rather than httpx, like every other request in the installer:
        # this module has to stay importable before a virtualenv exists.
        with urllib.request.urlopen(request, timeout=REGISTER_TIMEOUT_SECONDS) as answer:
            if answer.status < 300:
                info(f"registered with the head at {head}")
                return
            warn(f"the head at {head} answered {answer.status} to this registration.")
    except (urllib.error.URLError, OSError) as error:
        warn(f"could not register with the head at {head}: {error}")
        # Named because it is the likely cause and the least guessable one: the
        # head's app binds loopback and is published by `tailscale serve`, so
        # `http://<host>:8787` reaches nothing from another machine however
        # correct it looks.
        info("Is that the URL you actually open Workbench on? The head's app binds")
        info("127.0.0.1, so a bare http://<host>:8787 answers only on the head itself.")
        info("The node still serves models; its deploy timer will try again.")


def _head_argument() -> str | None:
    """The `--head` this install was given, in either spelling.

    Parsed by hand rather than with argparse because this module is reached
    through `install.sh` with the role flag still in `sys.argv`, and a parser
    strict enough to be useful would reject that.
    """
    argv = sys.argv[1:]
    for index, argument in enumerate(argv):
        if argument.startswith("--head="):
            return argument.split("=", 1)[1].strip().rstrip("/") or None
        if argument == "--head" and index + 1 < len(argv):
            return argv[index + 1].strip().rstrip("/") or None
    return None


def _stream_host_argument() -> str | None:
    """The `--stream-host` this install was given, or None if it said nothing.

    Same hand-parse and the same "None means leave it alone" rule as
    `--head`: a re-install that does not mention it must leave a client
    streaming from exactly what it streamed from before.
    """
    argv = sys.argv[1:]
    for index, argument in enumerate(argv):
        if argument.startswith("--stream-host="):
            return argument.split("=", 1)[1].strip().rstrip("/") or None
        if argument == "--stream-host" and index + 1 < len(argv):
            return argv[index + 1].strip().rstrip("/") or None
    return None


def _capabilities_argument() -> list[str] | None:
    """The `--capabilities` this install was given, or None if it said nothing.

    Same hand-parse as `_head_argument`, for the same reason, and safe through
    both re-execs for the same reason too: `become_root` re-execs with
    `*sys.argv[1:]` and `hand_off_to` comes back as the same module, which is
    exactly what `--head` already relies on. Unlike `--role`, this only
    parameterises the flow rather than selecting it, so argv is trust enough.

    An unrecognised name is refused here rather than dropped. `config` warns and
    ignores when *reading* the marker, because a machine that already has one
    must keep working; but a person typing the flag now is better told that
    `--capabilities=gaming,infrence` gave them a node that serves no models.
    """
    argv = sys.argv[1:]
    raw: str | None = None
    for index, argument in enumerate(argv):
        if argument.startswith("--capabilities="):
            raw = argument.split("=", 1)[1]
            break
        if argument == "--capabilities" and index + 1 < len(argv):
            raw = argv[index + 1]
            break
    if raw is None:
        return None

    named = [word.strip().lower() for word in raw.replace(",", " ").split()]
    unknown = [word for word in named if word not in CAPABILITIES]
    if unknown:
        raise InstallError(
            f"unknown capability {', '.join(repr(one) for one in unknown)}. "
            f"Known: {', '.join(CAPABILITIES)}"
        )
    if not named:
        raise InstallError("--capabilities was given nothing to offer")
    return named


def _gaming_user_argument() -> str | None:
    """Whose session Sunshine will run in, from the flag or from sudo.

    Defaults to `SUDO_USER`, which is right almost always: the person running
    `./install.sh --gaming` on their own laptop is the person who will play on
    it. When that is empty — already root, or a container — the flag is
    required rather than guessed.

    Guessing uid 1000 was the alternative and is worse than failing: it writes a
    Sunshine configuration into the wrong home, and the symptom is a stream that
    connects and captures nothing, which looks like a Sunshine bug for an
    afternoon.
    """
    argv = sys.argv[1:]
    for index, argument in enumerate(argv):
        if argument.startswith("--gaming-user="):
            return argument.split("=", 1)[1].strip() or None
        if argument == "--gaming-user" and index + 1 < len(argv):
            return argv[index + 1].strip() or None
    return os.environ.get("SUDO_USER", "").strip() or None


#: Ubuntu's own metapackage, which pulls in the i386 runtime Steam itself still
#: needs. Installing the actual client rather than reimplementing a launcher is
#: the same call already made for Ollama: the vendor's own package knows things
#: about its own dependencies that a hand-rolled unpack would get wrong.
STEAM_PACKAGE = "steam-installer"


def _nvidia_i386_gl_package() -> str | None:
    """The i386 package Steam actually needs to match this machine's driver.

    `dpkg --add-architecture i386` enables the architecture but does not pull
    in the 32-bit counterpart of a driver already installed in 64-bit, and
    Steam needs that counterpart to render anything hardware accelerated —
    without it, `apt-get install steam-installer` blocks on an interactive
    debconf prompt, which an unattended install must never hit.

    A first guess here, `nvidia-driver-libs:i386`, was never a real package —
    Ubuntu versions this per driver series instead: `libnvidia-gl-<series>`,
    where `<series>` is `nvidia-smi`'s own driver version up to its first dot
    (`595.91.07` -> `595`), the same series already installed in 64-bit.
    `None` when there is no driver at all to match, which the caller already
    checks for before ever reaching this.
    """
    probe = subprocess.run(
        ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return None
    series = probe.stdout.strip().split(".")[0]
    return f"libnvidia-gl-{series}:i386"


#: LizardByte's own Cloudsmith apt repository (added 2026-09-05, verified live
#: before this was written) — the same shape already proven for Moonlight-Qt's
#: own Cloudsmith feed. This replaces an earlier direct-`.deb`-download
#: approach pinned to one Ubuntu codename, which 404'd on real hardware: a
#: repo tracks codenames itself, a pinned URL does not.
SUNSHINE_CLOUDSMITH_SETUP = (
    "https://dl.cloudsmith.io/public/lizardbyte/stable/cfg/setup/bash.deb.sh"
)

#: Groups Sunshine's own documentation asks for: `input` for the virtual
#: controller/keyboard it presents to games, `video` for framebuffer access
#: underneath NvFBC, and `render` for the DRM render nodes every encoder
#: except NvFBC reaches for. Added, not assumed — checked against current
#: membership first, same narrow-privilege instinct as the polkit rule's
#: exact match.
#:
#: `render` was missing for a while and cost nothing visible, which is the
#: point worth recording. `/dev/dri/renderD*` is group `render`, not `video`
#: — two different groups on the same directory — so Sunshine could open
#: `card0` and not `renderD128`. Since NvFBC needs only the former, the whole
#: enumeration still "succeeded": VAAPI failed with `Permission denied`,
#: Sunshine logged it under its own "ignore any errors mentioned above"
#: banner, and fell through to a working encoder. A missing group that only
#: removes fallbacks is invisible until the path you were relying on is the
#: one that breaks.
SUNSHINE_GROUPS = ("input", "video", "render")

#: The unit the `sunshine` package actually installs — confirmed on real
#: hardware via `dpkg -L sunshine`. Not `sunshine.service`: LizardByte ships it
#: under its reverse-DNS app id, and a guessed plain name here means
#: `systemctl --user` reports "could not be found" and silently does nothing,
#: which is exactly what happened before this was checked.
SUNSHINE_UNIT_NAME = "app-dev.lizardbyte.app.Sunshine.service"


def _package_installed(name: str) -> bool:
    """Whether apt considers `name` installed.

    `dpkg-query` rather than `shutil.which`, because two of these packages
    (`pipewire-pulse`, `wireplumber`) are asked for by what they provide to
    other services rather than by a command anyone runs, and a `which` probe
    for them is a guess at a binary name.
    """
    probe = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0 and "install ok installed" in probe.stdout


def _in_group(player: str, group: str) -> bool:
    probe = subprocess.run(["id", "-nG", player], capture_output=True, text=True, check=False)
    return group in probe.stdout.split()


def _linger_enabled(player: str) -> bool:
    probe = subprocess.run(
        ["loginctl", "show-user", player, "--property=Linger", "--value"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.stdout.strip() == "yes"


def _ensure_linger(player: str) -> None:
    """Let `player`'s `systemd --user` instance run with nobody logged in.

    Sunshine is a real `--user` unit (unlike the render surface, which moved
    to a system unit for reasons that have nothing to do with this one): its
    manager has to exist before anyone has ever logged in as this account, and
    linger is the only thing that makes systemd start it anyway.
    """
    if _linger_enabled(player):
        return
    run(["loginctl", "enable-linger", player], privileged=True)
    info(f"enabled linger for '{player}', so Sunshine's session survives nobody logging in")


#: Sunshine ships `WantedBy=graphical-session.target`, which is right on a
#: desktop and unreachable here. A gaming node has no graphical session and
#: never will: its X server is the *system* unit `workbench-x11`, and nobody
#: ever logs in. So `systemctl --user enable` dutifully writes the symlink
#: into `graphical-session.target.wants/`, reports `enabled`, and the unit
#: cannot start at boot — because that target is never activated.
#:
#: This hid behind `enable --now` for as long as it existed. The `--now` half
#: starts Sunshine imperatively during the install, so a node was always
#: streaming by the time anyone checked, and `systemctl --user is-enabled`
#: said `enabled` the whole time. It was only ever the *next reboot* that
#: silently ended Moonlight streaming, with nothing in any log to say why.
#:
#: `default.target` is the one a lingering user manager actually reaches.
HEADLESS_DROPIN = (
    Path(".config/systemd/user") / f"{SUNSHINE_UNIT_NAME}.d" / "10-workbench-headless.conf"
)

#: Two edits, and the second is not cosmetic. Clearing `After=` drops the
#: ordering against targets this machine never reaches. The `ExecStartPre`
#: replaces Sunshine's packaged `sleep 5` with a wait for the X socket itself:
#: on a cold boot `workbench-x11` and this unit start together, and five
#: seconds is a guess about a race rather than an answer to it. Sunshine's own
#: `Restart=on-failure` allows five tries in 500 seconds, so losing that race
#: repeatedly does not mean a late start — it means a node that has given up
#: for good by the time the television is switched on.
HEADLESS_DROPIN_BODY = """\
[Unit]
# Rendered by install.sh. This node has no graphical session and never will:
# X here is the system unit workbench-x11, and nobody logs in.
After=

[Service]
# Wait for the display rather than guessing at it — see the installer.
ExecStartPre=
ExecStartPre=/bin/sh -c 'until [ -S /tmp/.X11-unix/X0 ]; do sleep 1; done'
"""


def _ensure_headless_autostart(account: pwd.struct_passwd) -> bool:
    """Make Sunshine start at boot on a machine with no graphical session.

    `add-wants` rather than an `[Install]` section in the drop-in: systemd
    reads `[Install]` from the unit file proper, and a drop-in carrying one is
    not a reliable way to change what `enable` links. `add-wants` writes the
    one symlink both would have written, and says so out loud.
    """
    target = Path(account.pw_dir) / HEADLESS_DROPIN
    changed = not target.is_file() or target.read_text() != HEADLESS_DROPIN_BODY
    if changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(HEADLESS_DROPIN_BODY)
        for path in (target, target.parent, target.parent.parent):
            os.chown(path, account.pw_uid, account.pw_gid)
        info(f"wrote {target}, so Sunshine survives a reboot")
        run_as_account(
            ["systemctl", "--user", "daemon-reload"],
            account,
            extra_env={"XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}"},
        )

    # Idempotent in systemd itself, so it runs every time rather than only
    # when the drop-in changed — the same lesson `enable --now` below records.
    result = run_as_account(
        ["systemctl", "--user", "add-wants", "default.target", SUNSHINE_UNIT_NAME],
        account,
        extra_env={"XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}"},
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        warn(f"could not add {SUNSHINE_UNIT_NAME} to default.target: {detail}")
    return changed


def install_steam() -> bool:
    """Put Steam on the machine.

    `dpkg --add-architecture i386` and enabling `multiverse` are real, standing
    changes to the package universe — not scoped to a venv or an account. Done
    only here, inside `is_gaming_node()`, and logged rather than silent: this
    is not something every node pays for, only ones that asked to play.
    """
    if shutil.which("steam") is not None:
        info("Steam already installed")
        return True

    architectures = subprocess.run(
        ["dpkg", "--print-foreign-architectures"], capture_output=True, text=True, check=False
    ).stdout.split()
    if "i386" not in architectures:
        info("enabling the i386 architecture for Steam")
        run(["dpkg", "--add-architecture", "i386"], privileged=True)
        run(["apt-get", "update"], privileged=True)

    if shutil.which("nvidia-smi") is not None:
        package = _nvidia_i386_gl_package()
        if package is None:
            warn("nvidia-smi did not report a driver version; skipped the i386 GL libs.")
        else:
            step(f"Installing {package}")
            try:
                run(["apt-get", "install", "-y", package], privileged=True, stream=True)
            except InstallError as error:
                # Best-effort: Steam's own postinst will ask for whatever it
                # actually needs if this guess was somehow still wrong.
                warn(f"could not install {package}: {error}")

    step(f"Installing {STEAM_PACKAGE}")
    try:
        result = run(["apt-get", "install", "-y", STEAM_PACKAGE], privileged=True, stream=True)
    except InstallError as error:
        warn(f"could not install {STEAM_PACKAGE}: {error}")
        return False
    if result.returncode != 0:
        warn(f"could not install {STEAM_PACKAGE}.")
        return False
    return True


def install_sunshine() -> bool:
    """Put Sunshine on the machine, and give the gaming user what it needs.

    LizardByte's own Cloudsmith apt repository — see `SUNSHINE_CLOUDSMITH_SETUP`'s
    own comment for why this replaced an earlier direct-`.deb`-download.
    """
    if shutil.which("sunshine") is not None:
        info("Sunshine already installed")
    else:
        step("Adding LizardByte's apt repository")
        try:
            run(
                ["sh", "-c", f"curl -1sLf {SUNSHINE_CLOUDSMITH_SETUP} | sudo -E bash"],
                stream=True,
            )
        except InstallError as error:
            warn(f"could not add LizardByte's apt repository: {error}")
            return False

        step("Installing Sunshine")
        try:
            result = run(["apt-get", "install", "-y", "sunshine"], privileged=True, stream=True)
        except InstallError as error:
            warn(f"could not install Sunshine: {error}")
            return False
        if result.returncode != 0:
            warn("could not install Sunshine.")
            return False

    player = gaming_user()
    if not player:
        warn("no gaming user recorded, so Sunshine's groups were not granted.")
        return True

    changed = False
    for group in SUNSHINE_GROUPS:
        if _in_group(player, group):
            continue
        run(["usermod", "-aG", group, player], privileged=True)
        info(f"added '{player}' to the '{group}' group")
        changed = True

    if changed:
        # Found the hard way: a `usermod` here does nothing for a process
        # that was already running when it ran — including this account's own
        # `systemd --user` manager, if `loginctl enable-linger` started it
        # earlier in this same install. Sunshine inherited the *old* group
        # list and could not open the render surface's devices until this ran.
        restart_user_manager(pwd.getpwnam(player))

    return True


#: Sunshine ships with no apps of its own, and Moonlight can only ask for an
#: app that exists — with an empty list there is nothing on the client to
#: select and so nothing that could ever start a stream. `"Desktop"` with no
#: `cmd` is Sunshine's own convention for "stream whatever is already on
#: screen" rather than launching something new, which is exactly this render
#: surface's whole job.
def _sunshine_apps_json() -> dict:
    return {
        "env": {},
        "apps": [
            {
                "name": "Desktop",
                "image-path": "desktop.png",
            }
        ],
    }


#: The prep commands go in `sunshine.conf`, NOT in `apps.json`, and that
#: distinction cost a working-looking switch that had never once fired.
#:
#: `apps.json` grew a top-level `global_prep_cmd` here first, on the strength
#: of documentation describing exactly that. Sunshine parsed the file, listed
#: the app, streamed happily — and silently ignored the key, because this
#: version reads global prep commands from `sunshine.conf` (they are edited on
#: the *Configuration* page of its web UI, not the *Applications* page). The
#: symptom was the worst available one: streaming worked, so nothing looked
#: broken, while every stream left Ollama running and the node still
#: advertising inference on a GPU a game was using.
#:
#: No `elevated` key: that field is Windows-only — Sunshine's own UI adds it
#: only when the platform is Windows — and on Linux the authorisation comes
#: from `install_gaming_rule`'s polkit grant instead.
def _sunshine_prep_commands(unit: str) -> str:
    commands = [{"do": f"systemctl start {unit}", "undo": f"systemctl stop {unit}"}]
    return json.dumps(commands, separators=(",", ":"))


def _sunshine_conf_with_prep(existing: str, unit: str) -> str:
    """`sunshine.conf` with our `global_prep_cmd` line, leaving every other
    setting alone.

    Rewritten rather than appended blindly, because Sunshine rewrites this
    whole file itself whenever anyone saves from its web UI — so this has to
    be something that converges when run twice, not something that stacks up a
    duplicate key per install.
    """
    kept = [
        line for line in existing.splitlines() if not line.lstrip().startswith("global_prep_cmd")
    ]
    while kept and not kept[-1].strip():
        kept.pop()
    kept.append(f"global_prep_cmd = {_sunshine_prep_commands(unit)}")
    return "\n".join(kept) + "\n"


def configure_sunshine_prep_command() -> bool:
    """Wire Sunshine into the switch: start it before a stream, stop it after.

    No sudo needed for either command — that is what `install_gaming_rule`'s
    polkit grant already buys the gaming user, which is why `elevated` is
    `False` rather than routing this through a password Sunshine has no way
    to supply.
    """
    player = gaming_user()
    if not player:
        return False
    try:
        account = pwd.getpwnam(player)
    except KeyError:
        warn(f"'{player}' is not a real account; Sunshine was not configured.")
        return False

    config_dir = Path(account.pw_dir) / ".config" / "sunshine"
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chown(config_dir, account.pw_uid, account.pw_gid)

    apps = config_dir / "apps.json"
    rendered = json.dumps(_sunshine_apps_json(), indent=4) + "\n"
    if apps.is_file() and apps.read_text() == rendered:
        info("Sunshine's app list already configured")
    else:
        apps.write_text(rendered)
        os.chown(apps, account.pw_uid, account.pw_gid)
        info(f"wrote {apps}, giving Moonlight something to select")

    conf = config_dir / "sunshine.conf"
    existing = conf.read_text() if conf.is_file() else ""
    wanted = _sunshine_conf_with_prep(existing, gaming_unit_name())
    if existing == wanted:
        info("Sunshine's prep command already configured")
    else:
        conf.write_text(wanted)
        os.chown(conf, account.pw_uid, account.pw_gid)
        info(f"wrote {conf}, wiring Sunshine into the gaming switch")

    _ensure_linger(player)
    _ensure_headless_autostart(account)

    # `enable --now`, not `restart`: a fresh install has never started this
    # unit at all, and `restart` on a unit that was never enabled starts it
    # for this boot only — every reboot would need a person to do this again.
    # `SUNSHINE_UNIT_NAME`, not the plain-looking guess `sunshine.service`:
    # see that constant's own comment for what a wrong name costs silently.
    #
    # Run every time, not only when apps.json above changed — found the hard
    # way that skipping it on an unchanged file leaves Sunshine
    # enabled-but-stopped whenever something *else* in this same install (a
    # group grant's `restart_user_manager`, most likely) stopped it in
    # between. `enable --now` on an already-active unit is a harmless no-op,
    # so there is no idempotency actually being bought by skipping it.
    result = run_as_account(
        ["systemctl", "--user", "enable", "--now", SUNSHINE_UNIT_NAME],
        account,
        extra_env={"XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}"},
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        warn(f"could not enable {SUNSHINE_UNIT_NAME} for '{player}': {detail}")
        return False
    return True


#: A gaming node has no sound card. PipeWire therefore comes up with no sink
#: at all, and Sunshine — which captures a sink's *monitor* — has nothing to
#: record, so every stream is silent.
#:
#: Found the same way as everything else here: the first real stream logged
#: `Couldn't connect to pulseaudio: Access denied` followed by `There will be
#: no audio`, and carried on delivering perfect video. Nothing failed, so
#: nothing said so.
#:
#: `pulseaudio-utils` is for the person diagnosing it rather than for
#: Sunshine: `pactl list short sinks` is the one command that answers "is
#: there anything to capture", and a node without it cannot be asked.
AUDIO_PACKAGES = ("pipewire", "pipewire-pulse", "wireplumber", "pulseaudio-utils")

#: A virtual sink for games to play into and Sunshine to read back. Sunshine
#: makes its own `sink-sunshine-stereo` per stream and restores the previous
#: default afterwards — this is the thing it restores *to*, and without one
#: there is no valid default for it to put back.
NULL_SINK_PATH = Path(".config/pipewire/pipewire.conf.d/10-workbench-null-sink.conf")

NULL_SINK_BODY = """\
# Rendered by install.sh. A gaming node has no sound card, so PipeWire starts
# with no sink and Sunshine has nothing to capture.
context.objects = [
    { factory = adapter
      args = {
          factory.name     = support.null-audio-sink
          node.name        = workbench-stream
          node.description = "Workbench stream"
          media.class      = Audio/Sink
          audio.position   = [ FL FR ]
          monitor.channel-volumes = true
      }
    }
]
"""

#: `pipewire.socket` and `pipewire-pulse.socket` alongside the services: the
#: sockets are what a client actually connects to, and Sunshine speaks the
#: PulseAudio protocol rather than PipeWire's own.
AUDIO_UNITS = (
    "pipewire.socket",
    "pipewire.service",
    "wireplumber.service",
    "pipewire-pulse.socket",
    "pipewire-pulse.service",
)


def install_audio() -> bool:
    """Give the gaming user an audio server, and something in it to capture.

    Depends on `_ensure_linger` having run, like Sunshine does: these are
    `--user` units on an account nobody logs into.
    """
    player = gaming_user()
    if not player:
        warn("no gaming user recorded, so no audio was configured.")
        return False
    try:
        account = pwd.getpwnam(player)
    except KeyError:
        warn(f"'{player}' is not a real account; no audio was configured.")
        return False

    missing = [name for name in AUDIO_PACKAGES if not _package_installed(name)]
    if missing:
        step(f"Installing {', '.join(missing)}")
        try:
            run(["apt-get", "install", "-y", *missing], privileged=True, stream=True)
        except InstallError as error:
            warn(f"could not install the audio stack: {error}")
            return False

    target = Path(account.pw_dir) / NULL_SINK_PATH
    if not target.is_file() or target.read_text() != NULL_SINK_BODY:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(NULL_SINK_BODY)
        for path in (target, target.parent, target.parent.parent):
            os.chown(path, account.pw_uid, account.pw_gid)
        info(f"wrote {target}, so a stream has something to capture")

    _ensure_linger(player)
    result = run_as_account(
        ["systemctl", "--user", "enable", "--now", *AUDIO_UNITS],
        account,
        extra_env={"XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}"},
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        warn(f"could not start the audio stack for '{player}': {detail}")
        return False
    return True


#: What a client node needs to put a stream on a screen. `moonlight-qt` is the
#: client; the rest is audio. `rtkit` is not optional in practice — Moonlight
#: raises its audio thread with `setpriority()`, and without rtkit granting
#: that, the thread competes with decoding and the stream crackles and drops
#: out. Found on real hardware, where it sounded like a network fault.
#: Build dependencies for Moonlight Embedded, plus the audio stack.
#:
#: Built from source rather than installed, because it is packaged for nothing
#: this runs on - its own apt repository serves a `trixie` suite that contains
#: no `moonlight-embedded` at all. The build is cheap (about two minutes on a
#: Pi 4) and the alternative was disqualifying: see `MOONLIGHT_EMBEDDED_REPO`.
CLIENT_PACKAGES = (
    "cmake",
    "gcc",
    "g++",
    "pkg-config",
    "git",
    "libasound2-dev",
    "libavahi-client-dev",
    "libcurl4-openssl-dev",
    "libevdev-dev",
    "libexpat1-dev",
    "libopus-dev",
    "libudev-dev",
    "libva-dev",
    "libvdpau-dev",
    "libpulse-dev",
    "uuid-dev",
    "libsdl2-dev",
    "libssl-dev",
    "libdrm-dev",
    "libavcodec-dev",
    "libavutil-dev",
    "libswscale-dev",
    "libavformat-dev",
    "pipewire",
    "pipewire-pulse",
    "wireplumber",
    "pulseaudio-utils",
)

#: Groups the account running the client needs: `video` and `render` to open
#: the DRM devices it renders through, `input` for the virtual gamepad it
#: presents, `audio` for the HDMI sink.
#:
#: `systemd-journal` is for the *doctor* rather than the unit, and is the
#: difference between a check and a decoration. `check_client_decoder` reads
#: back what the last stream did, and the doctor runs as the service account -
#: which without this cannot open the unit's journal at all and so reports
#: `unknown` forever, whatever the machine is really doing. A check that can
#: never answer is worse than no check, because it occupies the space where a
#: real answer would go.
#:
#: Granted only inside a client install, and worth stating plainly: it lets
#: this account read every journal on the machine, not only its own unit. On a
#: box whose whole job is to display a stream that is a fair trade; on a head
#: it would deserve more thought, which is why it is not granted there.
CLIENT_GROUPS = ("video", "render", "input", "audio", "systemd-journal")

CLIENT_UNIT_NAME = "workbench-client.service"

#: Moonlight Embedded, built from source. `moonlight-qt` was here first and is
#: disqualified, for one reason with three faces: **it cannot be driven from a
#: script.**
#:
#: It ignores `--video-decoder` on the command line. It ignores
#: `videodecoderselection` in its own config file. And its pairing PIN is shown
#: only in a GUI dialog - on a machine whose entire purpose is to have no
#: desktop, behind a modal warning that needs a keyboard to dismiss. Proven on
#: real hardware: the PIN was unobtainable over SSH by any means.
#:
#: Moonlight Embedded is the CLI-native client. `moonlight pair <host>` prints
#: the PIN to stdout and `moonlight stream <host>` needs no window system at
#: all, so every part of a client node can be operated from somewhere else -
#: which is the point of a machine nobody can stand in front of.
MOONLIGHT_EMBEDDED_REPO = "https://github.com/moonlight-stream/moonlight-embedded.git"

#: Where the source is built. Under the service account's home rather than the
#: checkout, because the checkout is what the deploy timer fast-forwards and a
#: build tree in it would make every deploy refuse as "dirty".
MOONLIGHT_BUILD_DIR = "moonlight-embedded"


def _client_unit(host: str, account: pwd.struct_passwd) -> str:
    """The unit that streams `host` onto this machine's screen.

    `SDL_VIDEODRIVER=kmsdrm` is the entire point, and is why a client node
    wants a *Lite* image with no desktop on it. Under a compositor the client
    cannot take DRM master, so its hardware-decode path fails and it falls
    back to decoding on the CPU — which on a Raspberry Pi 4 is the difference
    between a cheap appliance and a hot one. Learned the hard way on a machine
    running labwc: the client picked the hardware renderer, could not have the
    display, and showed a black screen while reporting success.

    Moonlight Embedded rather than `moonlight-qt`: see
    `MOONLIGHT_EMBEDDED_REPO` for why that is a requirement rather than a
    preference. It needs no window system, so there is no Qt platform to
    choose and no SDL video driver to hint at - which is also why this unit no
    longer sets `SDL_VIDEODRIVER`.
    """
    return f"""\
# Rendered by install.sh — do not edit; re-run the installer instead.
[Unit]
Description=Workbench: stream {host} onto this screen
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User={account.pw_name}
SupplementaryGroups={" ".join(CLIENT_GROUPS)}
# XDG_RUNTIME_DIR is for audio: PipeWire's socket lives there, and without it
# the stream plays perfect video in silence.
Environment=XDG_RUNTIME_DIR=/run/user/{account.pw_uid}
ExecStart=/usr/local/bin/moonlight stream {host} -app Desktop -1080 -fps 60 -bitrate 20000
Restart=on-failure
RestartSec=5
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
"""


def build_moonlight(account: pwd.struct_passwd) -> bool:
    """Build and install Moonlight Embedded from source.

    Idempotent by the only check that means anything here: whether the binary
    exists and runs. A source tree present but half-built is not a reason to
    skip, and a rebuild costs about two minutes.
    """
    if shutil.which("moonlight") is not None:
        info("Moonlight Embedded already installed")
        return True

    tree = Path(account.pw_dir) / MOONLIGHT_BUILD_DIR
    build = tree / "build"
    step("Building Moonlight Embedded from source")
    try:
        if not tree.exists():
            run_as_account(
                [
                    "git",
                    "clone",
                    "--recurse-submodules",
                    "--depth",
                    "1",
                    MOONLIGHT_EMBEDDED_REPO,
                    str(tree),
                ],
                account,
                stream=True,
            )
        # `sh -c` with an explicit cd: cmake and make are the two commands here
        # that care where they run, and `run_as_account` has no cwd of its own.
        run_as_account(
            ["sh", "-c", f"mkdir -p {build} && cd {build} && cmake .. && make -j4"],
            account,
            stream=True,
        )
        run(["sh", "-c", f"cd {build} && make install"], privileged=True, stream=True)
        # Without this the freshly installed binary cannot find its own
        # libraries in /usr/local/lib and dies on `libgamestream.so.4:
        # cannot open shared object file` - which looks like a broken build
        # rather than a stale linker cache.
        run(["ldconfig"], privileged=True)
    except InstallError as error:
        warn(f"could not build Moonlight Embedded: {error}")
        return False

    if shutil.which("moonlight") is None:
        warn("Moonlight Embedded built but is not on PATH.")
        return False
    return True


#: The card a client node's audio must come out of. PipeWire's own default on
#: a Raspberry Pi is the 3.5mm analogue jack, which on a machine wired to a
#: television by one HDMI cable is always wrong - and silently so: the stream
#: carries perfect audio to a socket with nothing in it. Found twice on real
#: hardware, once per Pi, because nothing anywhere reports it.
HDMI_SINK_HINT = "hdmi"


def prefer_hdmi_audio(account: pwd.struct_passwd) -> bool:
    """Make HDMI the default sink, so sound leaves by the same cable as picture."""
    env = {"XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}"}
    listing = run_as_account(["wpctl", "status"], account, extra_env=env)
    if listing.returncode != 0:
        warn("could not read the audio devices; left the default sink alone.")
        return False

    for line in listing.stdout.splitlines():
        if HDMI_SINK_HINT not in line.lower():
            continue
        # Lines look like "│      69. Built-in Audio Digital Stereo (HDMI) ..."
        match = re.search(r"(\d+)\.", line)
        if not match:
            continue
        sink = match.group(1)
        run_as_account(["wpctl", "set-default", sink], account, extra_env=env)
        run_as_account(["wpctl", "set-volume", sink, "0.9"], account, extra_env=env)
        run_as_account(["wpctl", "set-mute", sink, "0"], account, extra_env=env)
        info(f"audio will leave by HDMI (sink {sink})")
        return True

    warn("no HDMI audio sink found; the stream may be silent.")
    return False


def install_client() -> bool:
    """Everything a client node needs to show a stream.

    Deliberately not started here. Pairing with the machine it streams from is
    a one-time browser action nobody can automate — the same shape as joining
    the tailnet or signing the agent in — so the unit is installed and enabled
    and `report_outstanding` says what is left. Starting it before pairing
    would produce a unit that restarts every five seconds forever, which is a
    worse way to learn the same thing.
    """
    if not systemd_is_running():
        warn("no systemd here, so the client was not installed.")
        info("A real client node would get a unit that streams to its screen.")
        return False

    missing = [name for name in CLIENT_PACKAGES if not _package_installed(name)]
    if missing:
        step(f"Installing {len(missing)} packages for the client")
        try:
            run(
                ["apt-get", "install", "-y", "--no-install-recommends", *missing],
                privileged=True,
                stream=True,
            )
        except InstallError as error:
            warn(f"could not install the client's dependencies: {error}")
            return False

    account = _service_passwd()
    if not build_moonlight(account):
        return False

    changed = False
    for group in CLIENT_GROUPS:
        if _in_group(account.pw_name, group):
            continue
        run(["usermod", "-aG", group, account.pw_name], privileged=True)
        info(f"added '{account.pw_name}' to the '{group}' group")
        changed = True
    if changed:
        # Same lesson the gaming install records: a `usermod` does nothing for
        # an already-running manager, and this account's one may be running.
        restart_user_manager(account)

    _ensure_linger(account.pw_name)
    audio = run_as_account(
        ["systemctl", "--user", "enable", "--now", *AUDIO_UNITS],
        account,
        extra_env={"XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}"},
    )
    if audio.returncode != 0:
        warn(f"could not start audio: {(audio.stderr or audio.stdout or '').strip()}")
    else:
        prefer_hdmi_audio(account)

    host = stream_host()
    if not host:
        warn("no stream host recorded, so the client unit was not written.")
        info("Re-run with --stream-host <the machine to stream from>.")
        return False

    target = SYSTEMD_DIR / CLIENT_UNIT_NAME
    write_privileged(target, _client_unit(host, account), staged_as="workbench-client")
    run(["systemctl", "daemon-reload"], privileged=True)
    run(["systemctl", "enable", CLIENT_UNIT_NAME], privileged=True)
    info(f"installed {CLIENT_UNIT_NAME}, streaming from {host}")
    return True


def install_gaming() -> bool:
    """Everything a gaming node needs beyond the switch itself.

    In order: the switch's authorisation (existing), a render surface for
    Steam and Sunshine to use, Steam, Sunshine, an audio server for the stream
    to carry, and Sunshine's own wiring back into the switch. Each step is
    independent and degrades honestly — "Steam installed, Sunshine did not" is
    a real, reportable state, not a reason to abort the rest of the install.

    Audio goes before the prep command deliberately: that step is what leaves
    Sunshine running, and Sunshine picks its capture device at startup.
    """
    if not systemd_is_running():
        warn("no systemd here, so the gaming switch was not installed.")
        info("A real node would get a unit that hands its GPU to a game.")
        return False

    install_gaming_rule()
    render.install()
    install_steam()
    install_sunshine()
    install_audio()
    configure_sunshine_prep_command()
    return True


def main() -> int:
    configure_console_logging()
    os.chdir(repo_root())

    try:
        check_invocation()

        step("Checking prerequisites")
        check_prerequisites()
        # Only a node that will serve a model cares whether there is a card in
        # this machine. Asking on a client is how a Raspberry Pi gets told to
        # go install an NVIDIA driver.
        if is_inference_node():
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
        if (head := _head_argument()) is not None:
            record_head(head, account)
        # Only when asked. A re-install that says nothing must leave a node
        # offering exactly what it offered before, the way `--head` does.
        if (offering := _capabilities_argument()) is not None:
            record_capabilities(offering, account)
        if is_gaming_node() and (player := _gaming_user_argument()) is not None:
            record_gaming_user(player, account)
        if is_client_node() and (source := _stream_host_argument()) is not None:
            record_stream_host(source, account)

        # Everything below is a *capability*. `--role=node` on its own is the
        # smallest machine that can be reached, kept up to date and asked what
        # it is: a role marker, a head to report to, units and a deploy timer.
        # Each job a node does beyond that is declared, never assumed.
        #
        # This used to install the model server unconditionally, which was
        # fine while every node was an inference node and wrong the moment one
        # was not — an arm64 Raspberry Pi whose job is to *display* a stream
        # was still handed Ollama and a CUDA-shaped install it could not use.
        serving = False
        if is_inference_node():
            step("Installing the model server")
            serving = install_inference_server()
            if serving:
                pull_model()

        if is_gaming_node():
            step("Installing the gaming switch")
            install_gaming()

        if is_client_node():
            step("Installing the client")
            install_client()

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

        step("Registering with the head")
        register_with_head()

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
