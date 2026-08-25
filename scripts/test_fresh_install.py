#!/usr/bin/env python
"""The "Jake test": prove a stranger with a bare Ubuntu machine can clone this
repo, run install.sh, and get a working Workbench — with no steps that exist
only in someone's head.

    uv run scripts/test_fresh_install.py                # test the working tree
    uv run scripts/test_fresh_install.py --from-github  # test what a stranger gets

Run through `uv run` (or with the virtualenv active): this imports workbench
for its console logging.

The working tree is the default so uncommitted work can be checked before it is
pushed. --from-github clones the public repo, which is the actual claim.

Docker rather than LXD: no host-side initialisation, and it is what CI runners
provide, so this same script can gate CI.

Caveat: a plain container has no systemd, so install.sh skips the service step
here. This exercises everything up to and including a working app; the systemd
leg is covered by installing on the real server.
"""

import argparse
import io
import logging
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from workbench.logs import BOLD, GREEN, RED, configure_console_logging, paint

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
GITHUB_URL = "https://github.com/idm23/workbench.git"
TARGET = "/opt/workbench"
DEFAULT_IMAGE = "ubuntu:26.04"

# Never shipped into the container: build artefacts and history that would make
# the transfer huge and, more importantly, would let the install cheat by
# reusing a virtualenv or database that a fresh clone would not have.
EXCLUDED = {".venv", "data", ".git", "__pycache__", ".pytest_cache", ".ruff_cache"}


class TestFailureError(Exception):
    pass


def step(message: str) -> None:
    logger.info("\n%s", paint(BOLD, f"==> {message}"))


def docker(*args: str, check: bool = True) -> int:
    """Run a docker command, letting its output stream to the console.

    Streaming rather than capturing matters here: install.sh's progress is the
    most useful thing to watch when this fails.
    """
    returncode = subprocess.run(["docker", *args]).returncode
    if check and returncode != 0:
        raise TestFailureError(f"`docker {' '.join(args[:3])} ...` failed")
    return returncode


def docker_quiet(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise TestFailureError(f"`docker {' '.join(args[:3])} ...` failed\n{result.stderr.strip()}")
    return result.stdout.strip()


def docker_send(*args: str, data: bytes) -> None:
    result = subprocess.run(["docker", *args], input=data)
    if result.returncode != 0:
        raise TestFailureError(f"`docker {' '.join(args[:3])} ...` failed")


def require_docker() -> None:
    if shutil.which("docker") is None:
        raise TestFailureError("docker is required to run this test.")

    if subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
        return

    # A very common cause is a selected-but-not-running Docker Desktop context
    # while the system daemon is up. Say so rather than leaving it to guesswork.
    fallback = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        env={**os.environ, "DOCKER_CONTEXT": "default"},
    )
    hint = ""
    if fallback.returncode == 0:
        hint = (
            "\n\n       The 'default' context works but is not selected. Either:\n"
            "           DOCKER_CONTEXT=default uv run scripts/test_fresh_install.py\n"
            "       or switch permanently:\n"
            "           docker context use default"
        )
    raise TestFailureError(f"docker is installed but not usable.{hint}")


def _without_build_artefacts(entry: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if EXCLUDED & set(Path(entry.name).parts):
        return None
    return entry


def worktree_tarball() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for entry in sorted(REPO_ROOT.iterdir()):
            if entry.name in EXCLUDED:
                continue
            archive.add(entry, arcname=entry.name, filter=_without_build_artefacts)
    return buffer.getvalue()


#: Where the install's own output is kept inside the container, so it can be
#: both watched live and asserted on afterwards.
INSTALL_LOG = "/tmp/install.log"


def run_test(image: str, from_github: bool, container: str) -> None:
    step(f"Starting a clean {image} container")
    docker_quiet("run", "-d", "--name", container, image, "sleep", "infinity")
    logger.info("    %s", container)

    step("Installing base tools (what a fresh Ubuntu does not ship)")
    docker(
        "exec",
        container,
        "bash",
        "-c",
        "export DEBIAN_FRONTEND=noninteractive && "
        "apt-get update -qq >/dev/null && "
        "apt-get install -y -qq curl git ca-certificates >/dev/null",
    )

    if from_github:
        step("Cloning from GitHub")
        docker("exec", container, "git", "clone", "--depth", "1", GITHUB_URL, TARGET)
    else:
        step("Copying the working tree in")
        docker("exec", container, "mkdir", "-p", TARGET)
        docker_send(
            "exec", "-i", container, "tar", "xzf", "-", "-C", TARGET, data=worktree_tarball()
        )

    step("Running install.sh")
    # Through `tee` rather than captured, because install.sh's progress is the
    # most useful thing to watch when this fails — and `pipefail` because
    # otherwise the pipeline reports tee's exit code and a failed install
    # sails through as a pass.
    docker(
        "exec",
        container,
        "bash",
        "-c",
        f"set -o pipefail; cd {TARGET} && ./install.sh 2>&1 | tee {INSTALL_LOG}",
    )
    install_output = docker_quiet("exec", container, "cat", INSTALL_LOG)

    step("Confirming the install named the steps it cannot do for you")
    # The reproducibility rule does not promise every step is automatable — it
    # promises none is undiscoverable. Minting the agent's credential needs a
    # browser login against an account no script can know, so the bar it has to
    # clear is that the installer says so, out loud, with the command. This
    # assertion is the whole of that promise: without it the step silently
    # slips back out of the output and is rediscovered by a failed run, which
    # is exactly how it was found the first time.
    if "auth login" not in install_output:
        raise TestFailureError("the install did not tell anyone how to authenticate the agent")

    step("Starting the app (no systemd in a container)")
    docker(
        "exec",
        "-d",
        container,
        "bash",
        "-c",
        f"cd {TARGET} && .venv/bin/uvicorn workbench.app:app "
        "--host 127.0.0.1 --port 8787 >/tmp/uvicorn.log 2>&1",
    )

    step("Smoke testing inside the container")
    smoke = docker(
        "exec",
        container,
        "bash",
        "-c",
        f"cd {TARGET} && .venv/bin/python scripts/smoke_test.py",
        check=False,
    )
    if smoke != 0:
        logger.error("\n--- uvicorn log ---")
        docker("exec", container, "cat", "/tmp/uvicorn.log", check=False)
        raise TestFailureError("smoke test failed")

    step("Re-running install.sh (it must be safe to repeat)")
    docker("exec", container, "bash", "-c", f"cd {TARGET} && ./install.sh")

    step("Confirming data survived the second install")
    survived = docker(
        "exec",
        container,
        "bash",
        "-c",
        "curl -s http://127.0.0.1:8787/ | grep -q smoketest-",
        check=False,
    )
    if survived != 0:
        raise TestFailureError("the user created before the re-install is gone")


def main() -> int:
    configure_console_logging()

    # __doc__ is None under `python -OO`; argparse accepts None, so pass it
    # through rather than indexing into it.
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--from-github",
        action="store_true",
        help="clone the public repo instead of using the working tree",
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    arguments = parser.parse_args()

    container = f"workbench-freshtest-{os.getpid()}"
    try:
        require_docker()
        run_test(arguments.image, arguments.from_github, container)
    except TestFailureError as failure:
        logger.error("\n%s %s\n", paint(RED, "error:"), failure)
        return 1
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)

    source = "github" if arguments.from_github else "worktree"
    logger.info(
        "\n%s Source: %s, image: %s\n",
        paint(GREEN, "Fresh install works."),
        source,
        arguments.image,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
