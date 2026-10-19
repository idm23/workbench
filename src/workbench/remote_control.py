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

With `--project <id>` it is a project's session instead: the spec the
deployer wrote (`workbench.project_sessions`) says which clone to work in,
which login to sign in as, and what to call it.
"""

import logging
import os
import pty
import sys
from pathlib import Path

from workbench.agents.claude import cli_config_file, remote_control_argv, skip_first_run_setup
from workbench.config import agent_environment
from workbench.logs import configure_console_logging
from workbench.project_sessions import read_spec

logger = logging.getLogger(__name__)


def _enter_project(project_id: int) -> str | None:
    """Become project `project_id`'s session: its clone, its login, its
    environment. Returns the session's name, or None without a spec."""
    spec = read_spec(project_id)
    if spec is None:
        return None
    os.chdir(spec.cwd)
    # `pty.spawn` execs with this process's environment, so it is set here.
    # The clone's scratch database rather than this instance's, as for runs.
    env = agent_environment(worktree=Path(spec.cwd))
    if spec.config_dir is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = spec.config_dir
    os.environ.clear()
    os.environ.update(env)
    return spec.name


def main(argv: list[str] | None = None) -> int:
    configure_console_logging()
    args = sys.argv[1:] if argv is None else argv

    name: str | None = None
    if args[:1] == ["--project"] and len(args) == 2:
        name = _enter_project(int(args[1]))
        if name is None:
            logger.error("No session spec for project %s; nothing to start.", args[1])
            return 1

    command = remote_control_argv(name)
    if command is None:
        logger.error("No Claude CLI found; the remote-control session cannot start.")
        return 1

    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        skip_first_run_setup(cli_config_file(Path(config_dir) if config_dir else None), Path.cwd())
    except (OSError, ValueError) as error:
        # Starting anyway is no worse than before: it waits at setup.
        logger.warning("Could not pre-answer the CLI's first-run setup: %s", error)

    logger.info("Starting remote-control session: %s", " ".join(command))
    status = pty.spawn(command)

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
