"""Which commit the checkout is sitting on.

Small enough to have been written inline three times before this existed. It
lives here rather than in `config` because it asks git, not the environment.

Deliberately uncached: the deployer reads this either side of a fast-forward,
so a cached answer would report the old revision as the new one. Callers for
whom it genuinely cannot change — a running web process, which a deploy always
restarts — cache it themselves.
"""

import subprocess

from workbench.config import repo_root

#: Reading a revision is a local operation against a file on disk. If it has
#: not answered by now, something is wrong that waiting will not fix.
TIMEOUT_SECONDS = 15

#: What to report when git cannot say. A tarball with no `.git`, or a checkout
#: git refuses to read — neither is worth failing a health check over, and
#: "unknown" is more honest than omitting the field.
UNKNOWN = "unknown"


def head_revision(short: bool = True) -> str:
    """The commit at HEAD, or `unknown` if git cannot say.

    Never raises. Every caller is either answering a health check or writing a
    log line, and neither should fail because a subprocess did.
    """
    argv = ["git", "rev-parse"]
    if short:
        argv.append("--short")
    argv.append("HEAD")

    try:
        result = subprocess.run(
            argv,
            cwd=repo_root(),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return UNKNOWN

    if result.returncode != 0:
        return UNKNOWN
    return result.stdout.strip() or UNKNOWN
