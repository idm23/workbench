"""ExecStart for the standing remote-control session: `python -m workbench.remote_control`.

A systemd unit can only run a static command, and the command here is not
static — it has to ask `agents.claude` where the CLI binary actually lives,
which is SDK-internal knowledge that module alone is allowed to hold (see
`agents/tests/test_seam.py`). So the unit runs this instead, which resolves
that once at process start and then becomes the thing it is asking for.

The second reason this is a module rather than a raw `ExecStart=claude
--remote-control` is more basic: `--remote-control` starts a genuinely
interactive session, the same one you would get typing `claude` at a
terminal, and a systemd unit has no terminal. Tried directly, with stdin
pointed at `/dev/null` the way a unit's is by default, the CLI refused
immediately — "Input must be provided either through stdin or as a prompt
argument when using --print" — rather than opening its interface at all.
`pty.spawn` is the stdlib's own answer to exactly this: it allocates a real
pseudo-terminal for the child regardless of what the parent's own stdin is,
which is what let a plain, non-interactive probe of this exact invocation
render the CLI's first-run setup screen correctly.
"""

import logging
import os
import pty

from workbench.agents.claude import remote_control_argv
from workbench.logs import configure_console_logging

logger = logging.getLogger(__name__)


def main() -> int:
    configure_console_logging()

    argv = remote_control_argv()
    if argv is None:
        logger.error("No Claude CLI found; the remote-control session cannot start.")
        return 1

    logger.info("Starting remote-control session: %s", " ".join(argv))
    status = pty.spawn(argv)

    # `pty.spawn` returns an `os.wait`-style status rather than a plain exit
    # code, so systemd would otherwise see this unit's own composite number
    # rather than the wrapped command's — Restart=always only needs "did it
    # exit", so a signal is folded into the same non-zero rather than
    # reported more precisely.
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
