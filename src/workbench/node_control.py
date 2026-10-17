"""The buttons a head can press on a node.

A node runs no web app, and this is not one: it is two verbs, `start` and
`stop`, for the one thing a person wants to do to a node from a phone — put
the game on the television, and take it off again. Run by
`workbench-node.service` as the service account.

**Fixed verbs, no arguments.** The listener is unauthenticated on every
interface, like the model server beside it, so the whole safety argument is
that a request cannot say anything that changes *what* runs — only whether it
does. Each verb maps to `systemctl start` or `stop` on units named here, and
polkit grants the service account exactly those units and nothing else.

What each verb does depends on what the node offers, read the same way the
installer and the doctor read it:

- **gaming**: `start` makes sure the gaming account's Steam is running.
  Never restarted: `start` on a running unit does nothing, and pressing Play
  must not end a game in progress. `stop` leaves Steam alone for the same reason.
- **client**: `start` and `stop` the stream onto this screen.

On a node that is both, Steam goes first, so the stream opens onto it.
"""

import json
import logging
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from workbench.config import (
    CLIENT_UNIT_NAME,
    CONTROL_PORT,
    gaming_user,
    is_client_node,
    is_gaming_node,
    steam_up_unit_name,
)
from workbench.logs import configure_console_logging

logger = logging.getLogger(__name__)

#: Longer than the Steam unit's own start timeout, so systemd answers first and
#: says why rather than this giving up without a reason.
SYSTEMCTL_TIMEOUT_SECONDS = 40

VERBS = ("start", "stop")


def _systemctl(verb: str, unit: str) -> str | None:
    """Run one `systemctl` verb on one unit. None on success, else why not."""
    try:
        finished = subprocess.run(
            ["systemctl", verb, unit],
            capture_output=True,
            text=True,
            check=False,
            timeout=SYSTEMCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"{verb} {unit}: {error}"
    if finished.returncode != 0:
        detail = (finished.stderr or finished.stdout or "").strip()
        return f"{verb} {unit}: {detail or f'exit {finished.returncode}'}"
    return None


def plan(verb: str) -> list[tuple[str, str]]:
    """The (systemctl verb, unit) pairs a verb means on this node, in order."""
    steps: list[tuple[str, str]] = []
    if verb == "start" and is_gaming_node() and gaming_user():
        steps.append(("start", f"{steam_up_unit_name()}.service"))
    if is_client_node():
        steps.append((verb, CLIENT_UNIT_NAME))
    return steps


def press(verb: str) -> dict[str, object]:
    """Carry out a verb. Every step is attempted; any failure is reported."""
    done: list[str] = []
    failed: list[str] = []
    for action, unit in plan(verb):
        problem = _systemctl(action, unit)
        if problem is None:
            done.append(f"{action} {unit}")
            logger.info("%s %s", action, unit)
        else:
            failed.append(problem)
            logger.warning("%s", problem)
    return {"ok": not failed, "done": done, "failed": failed}


class Handler(BaseHTTPRequestHandler):
    server_version = "workbench-node"

    def _reply(self, status: HTTPStatus, body: dict[str, object]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        prefix, _, verb = self.path.rpartition("/")
        if prefix != "/stream" or verb not in VERBS:
            self._reply(
                HTTPStatus.NOT_FOUND, {"ok": False, "failed": [f"no such button: {self.path}"]}
            )
            return
        if not plan(verb):
            self._reply(
                HTTPStatus.CONFLICT,
                {"ok": False, "failed": ["this node offers nothing to stream"]},
            )
            return
        outcome = press(verb)
        self._reply(HTTPStatus.OK if outcome["ok"] else HTTPStatus.BAD_GATEWAY, outcome)

    def log_message(self, format: str, *args: object) -> None:
        # Into the journal through logging, rather than stderr's own format.
        logger.info("%s %s", self.address_string(), format % args)


def main() -> None:
    configure_console_logging()
    server = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), Handler)
    logger.info("Answering the head's buttons on port %d.", CONTROL_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
