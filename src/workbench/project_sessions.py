"""Which projects get a standing remote-control session, and as whom.

`python -m workbench.project_sessions write` is run by the deployer as the
service account, never as root: it is the one step of that convergence that
reads the database, and root opening it would leave root-owned `-wal` and
`-shm` files the app could no longer write. It writes one spec per project to
`project_sessions_dir()` and prints `{id: fingerprint}` for the deployer, which
then does the part only root can — enabling, restarting and stopping
`<prefix>@<id>.service`. The session itself reads its spec at start
(`python -m workbench.remote_control --project <id>`).

A project gets a session when it has a clone here and may use Claude, signed
in as the project's own login (`lifecycle.project_claude_login`): the work
account for a project restricted to it, the default login otherwise. A named
login that has never been created is skipped rather than started, because it
would sit at the CLI's sign-in screen — the doctor's job to report, not a
unit's to wait at.
"""

import hashlib
import json
import logging
import socket
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select

from workbench.config import claude_login_dir, project_sessions_dir
from workbench.database.db import session_scope
from workbench.database.models import Project
from workbench.git.worktrees import local_checkout
from workbench.logs import configure_console_logging
from workbench.runs.lifecycle import project_claude_login

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionSpec:
    """Everything a project's session needs, so it never opens the database."""

    name: str
    cwd: str
    #: A named login's directory; None is the default login.
    config_dir: str | None

    def fingerprint(self) -> str:
        """Changes exactly when a running session would need restarting."""
        encoded = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]


def spec_for(project: Project) -> SessionSpec | None:
    checkout = local_checkout(project.owner, project.repo)
    if checkout is None:
        return None
    login = project_claude_login(project)
    if login is None:
        return None
    config_dir: Path | None = None
    if login.name is not None:
        config_dir = claude_login_dir(login.name)
        if config_dir is None:
            logger.warning(
                "%s/%s: login %r has not been created here; no session.",
                project.owner,
                project.repo,
                login.name,
            )
            return None
    return SessionSpec(
        name=f"{project.repo} on {socket.gethostname()}",
        cwd=str(checkout),
        config_dir=None if config_dir is None else str(config_dir),
    )


def wanted_specs() -> dict[int, SessionSpec]:
    with session_scope() as db:
        projects = db.scalars(select(Project).order_by(Project.id)).all()
        return {p.id: spec for p in projects if (spec := spec_for(p)) is not None}


def spec_path(project_id: int) -> Path:
    return project_sessions_dir() / f"{project_id}.json"


def read_spec(project_id: int) -> SessionSpec | None:
    try:
        return SessionSpec(**json.loads(spec_path(project_id).read_text()))
    except OSError, ValueError, TypeError:
        return None


def write_specs(specs: dict[int, SessionSpec]) -> None:
    """Replace the directory's contents with exactly these specs."""
    directory = project_sessions_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.json"):
        if stale.stem not in {str(i) for i in specs}:
            stale.unlink()
    for project_id, spec in specs.items():
        staged = directory / f".{project_id}.json.tmp"
        staged.write_text(json.dumps(asdict(spec), indent=2))
        staged.replace(spec_path(project_id))


def main(argv: list[str]) -> int:
    configure_console_logging()
    if argv != ["write"]:
        sys.stderr.write("usage: python -m workbench.project_sessions write\n")
        return 2
    specs = wanted_specs()
    write_specs(specs)
    # Last, after any warning: the deployer reads the final line of stdout.
    sys.stdout.write(json.dumps({str(i): spec.fingerprint() for i, spec in specs.items()}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
