"""Hand this node's GPU to the screen downstairs, and take it back afterwards.

A node with 8 GB of VRAM cannot hold `qwen3:8b` and a game at the same time, so
inference and gaming are mutually exclusive on one card. This module is the
switch, driven by `workbench-gaming.service` and, in practice, by Sunshine's
`global_prep_cmd` on either side of a stream.

**It is an optimisation, not the mechanism.** What actually stops the head
dispatching a run here is `install_node.capabilities()`, which withdraws
`inference` whenever the model server is not answering — derived from state, so
the deploy timer's five-minute re-registration agrees with it instead of
clobbering it. Without this module the head would still notice, within five
minutes. With it, within seconds. That split is deliberate: a mechanism that
depends on a hook firing is worth less than one that survives the hook failing.

Which is also why `start` does not merely *say* the node is busy. It stops the
model server, because a game needs the VRAM back rather than a promise, and then
re-registers so the head hears about it now rather than at the next tick.
"""

import argparse
import logging
import subprocess
import sys
import time

from workbench.config import inference_base_url
from workbench.install_node import _endpoint_answers, register_with_head
from workbench.logs import configure_console_logging

logger = logging.getLogger(__name__)

#: The unit whose VRAM this is arguing over. Ollama's own unit name, not one of
#: ours — we drive it rather than reimplementing it, and this is the one place
#: that has to know what it is called.
INFERENCE_UNIT = "ollama.service"

#: How long to wait for the model server to actually let go of the card, or to
#: come back. Bounded because Sunshine blocks on `start` before it launches the
#: game: a person is standing at a television, and an unbounded wait here is a
#: black screen with no explanation.
SETTLE_TIMEOUT_SECONDS = 10.0

#: How often to re-ask while waiting. Frequent: the whole budget is ten seconds.
SETTLE_INTERVAL_SECONDS = 0.25


def _systemctl(*arguments: str) -> bool:
    """Ask the system manager to do something, and never raise.

    Never fatal, on either path, and the two reasons differ. A `stop` that fails
    means the game gets a card with a model on it — degraded, not broken, and
    better than refusing to launch the game at all. A `start` that fails means
    inference stays down, which `capabilities()` already reports honestly to the
    head. Both are worth a log line and neither is worth a traceback in front of
    somebody holding a controller.
    """
    argv = ["systemctl", *arguments]
    try:
        finished = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("Could not run %s: %s", " ".join(argv), error)
        return False

    if finished.returncode != 0:
        detail = (finished.stderr or finished.stdout or "").strip()
        logger.warning("%s failed: %s", " ".join(argv), detail)
        return False
    return True


def _wait_until(serving: bool) -> bool:
    """Wait for the model server to reach a state, or give up saying so."""
    deadline = time.monotonic() + SETTLE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _endpoint_answers() is serving:
            return True
        time.sleep(SETTLE_INTERVAL_SECONDS)
    return False


def start() -> int:
    """Take the GPU: stop the model server, then tell the head about it.

    In that order, and the order is the point. Registering first would announce
    a node that still had 5 GB of weights resident, which is exactly the claim
    this exists to stop being false.
    """
    logger.info("Handing this node's GPU over: stopping %s.", INFERENCE_UNIT)
    _systemctl("stop", INFERENCE_UNIT)

    if not _wait_until(serving=False):
        # Worth saying and not worth failing over. The game still launches, and
        # `capabilities()` will report whatever is actually true.
        logger.warning(
            "%s still answers at %s after %.0fs; the game may be short of VRAM.",
            INFERENCE_UNIT,
            inference_base_url(),
            SETTLE_TIMEOUT_SECONDS,
        )

    register_with_head()
    return 0


def stop() -> int:
    """Give the GPU back: start the model server, then tell the head.

    Same ordering argument reversed — announcing inference before the server
    answers would hand the head a node that fails the next run it is given.
    """
    logger.info("Taking this node's GPU back: starting %s.", INFERENCE_UNIT)
    _systemctl("start", INFERENCE_UNIT)

    if not _wait_until(serving=True):
        logger.warning(
            "%s did not answer at %s within %.0fs; this node stays withdrawn.",
            INFERENCE_UNIT,
            inference_base_url(),
            SETTLE_TIMEOUT_SECONDS,
        )

    # Either way. A registration that says "still not serving" is the useful
    # thing to send, because the alternative is a head that believes the node
    # recovered and sends it the next run.
    register_with_head()
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_console_logging()
    parser = argparse.ArgumentParser(
        prog="python -m workbench.gaming",
        description="Hand this node's GPU to a game, or take it back.",
    )
    parser.add_argument("action", choices=("start", "stop"))
    action = parser.parse_args(sys.argv[1:] if argv is None else argv).action
    return start() if action == "start" else stop()


if __name__ == "__main__":
    raise SystemExit(main())
