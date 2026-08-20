"""Console output for the command-line entry points.

These tools' output is read by a person, so the formatter emits the bare
message rather than a log record — no level prefixes or timestamps. Going
through logging rather than print still buys three things: level filtering, one
place to change where output goes, and a handler that library log records flow
through, so their output and ours stay interleaved correctly.
"""

import logging
import os
import sys

BOLD = "1"
RED = "31"
GREEN = "32"
YELLOW = "33"

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

# Libraries that log at INFO on every operation. Useful when debugging them,
# pure noise in an installer's output.
# `alembic.runtime.plugins` announces every autogenerate plugin it registers,
# at import time rather than on use — so merely importing anything that touches
# alembic emits seven lines. alembic.ini quiets this for the alembic CLI, but
# that config belongs to the CLI's own process; anything importing the library
# has to quiet it here.
_NOISY_LOGGERS = ("httpx", "httpcore", "alembic.runtime.plugins")


def paint(code: str, text: str) -> str:
    """Wrap text in an ANSI colour, unless output is piped or NO_COLOR is set."""
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def configure_console_logging(level: int = logging.INFO) -> None:
    """Send everything to stdout as plain text.

    One stream, not the usual stdout/stderr split. This output is a narrative
    meant to be read top to bottom, and two streams through a pipe — or through
    `docker exec`, which multiplexes them independently — arrive interleaved out
    of order, so a warning can surface after the line that follows it. The exit
    code remains the machine-readable signal.
    """
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
