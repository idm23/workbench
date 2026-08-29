"""Where a run executes, behind one seam.

The runner decides *what* happens during a run. This decides *where the process
lives*, which is a separate question with a separate answer per machine — and,
before long, per job: a repository task belongs on this box, and something that
wants a GPU belongs on a node that has one.

Shaped deliberately like `workbench.agents`. An executor has a name that goes in
`runs.executor`, and it hands back an opaque `handle` that goes in `runs.handle`
and means nothing without that name. A unit name today, a pid for the fallback,
a remote job id later. Nothing outside an executor parses a handle, which is
what lets a third one arrive as a new class rather than a refactor.

Why units rather than child processes is `docs/running-agents.md`, and it is
worth reading before this file: the reason is control groups, not tidiness.
"""

import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from workbench.config import (
    DEFAULT_EXECUTOR,
    SYSTEMD_EXECUTOR,
    default_executor,
    repo_root,
    run_unit_name,
)

logger = logging.getLogger(__name__)

#: Long enough for systemd to answer over D-Bus, short enough that a wedged
#: manager does not hold a web request open.
SYSTEMCTL_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class Started:
    """The run is executing, and `handle` is how to reach it again."""

    handle: str


@dataclass(frozen=True)
class StartRefused:
    """It could not be started, and nothing is running.

    A result rather than an exception because every realistic cause is
    ordinary: no systemd here, the polkit rule missing, the unit template not
    installed yet. The caller records the message on the run and moves on.
    """

    message: str


type StartResult = Started | StartRefused


@runtime_checkable
class Executor(Protocol):
    """Somewhere a run can be carried out."""

    @property
    def name(self) -> str:
        """The identifier stored in `runs.executor`."""
        ...

    def start(self, run_id: int) -> StartResult: ...

    def cancel(self, handle: str) -> bool:
        """Ask it to stop. True if the request was accepted.

        Asking, not killing: the runner catches SIGTERM to record `cancelled`
        and let the agent's own child wind up, so a polite stop produces a
        legible run and an abrupt one does not.
        """
        ...

    def is_running(self, handle: str) -> bool:
        """Whether it is still going.

        This is what makes reaping possible. A run whose row says `running`
        while its executor says otherwise died without recording an outcome,
        and something has to notice.
        """
        ...


def _python() -> str:
    """The interpreter runs are started with.

    The virtualenv's, not `sys.executable`: a run may be started from a
    process that is not itself in the venv — the deployer, a shell — and the
    runner needs the venv's dependencies either way.
    """
    venv = repo_root() / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


class SystemdUnitExecutor:
    """One transient-ish systemd unit per run, from an installed template.

    `systemctl start workbench-run@42.service` — a real unit, in its own
    control group, which is the entire point: stopping the web service kills
    everything in *its* cgroup, and this is not in it.

    A template unit rather than `systemd-run --property=...` because the unit
    file is a better place for the parts that will grow. Resource limits,
    hardening, and later device access for a GPU node are declarative, reviewed
    in a pull request, and version-controlled in `deploy/` — rather than
    assembled as command-line arguments here.
    """

    @property
    def name(self) -> str:
        return SYSTEMD_EXECUTOR

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["systemctl", *args],
                capture_output=True,
                text=True,
                timeout=SYSTEMCTL_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            logger.warning("systemctl %s failed: %s", " ".join(args), error)
            return None

    def start(self, run_id: int) -> StartResult:
        unit = run_unit_name(run_id)
        # --no-block: the call returns once systemd has accepted the job, not
        # once the agent has finished. A run lasts minutes and this is called
        # from a web request.
        result = self._systemctl("start", "--no-block", unit)
        if result is None:
            return StartRefused("systemctl is not available on this machine.")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            return StartRefused(f"Could not start {unit}: {detail}")
        return Started(unit)

    def cancel(self, handle: str) -> bool:
        result = self._systemctl("stop", "--no-block", handle)
        return result is not None and result.returncode == 0

    def is_running(self, handle: str) -> bool:
        result = self._systemctl("is-active", handle)
        if result is None:
            return False
        # `is-active` exits non-zero for anything but active, and prints the
        # state either way. `activating` counts: the unit exists and is on its
        # way up, so treating it as dead would reap a run that is about to run.
        return result.stdout.strip() in ("active", "activating", "reloading")


class LocalProcessExecutor:
    """A detached child process. The fallback where there is no systemd.

    Honest about what it is: this is what the container the fresh-install test
    runs in gets, and what someone developing on a laptop gets. It does not
    survive the web process being restarted — nothing forked from a unit does —
    which is exactly why it is not the answer on the server.
    """

    @property
    def name(self) -> str:
        return DEFAULT_EXECUTOR

    def start(self, run_id: int) -> StartResult:
        try:
            process = subprocess.Popen(
                [_python(), "-m", "workbench.runs.runner", str(run_id)],
                cwd=repo_root(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Detaches from the terminal's session, which is worth having
                # even though it does not detach from a cgroup.
                start_new_session=True,
            )
        except OSError as error:
            return StartRefused(f"Could not start the runner: {error}")
        return Started(str(process.pid))

    def _pid(self, handle: str) -> int | None:
        try:
            return int(handle)
        except ValueError:
            logger.warning("Not a pid: %r", handle)
            return None

    def cancel(self, handle: str) -> bool:
        pid = self._pid(handle)
        if pid is None:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError, PermissionError:
            return False
        return True

    def is_running(self, handle: str) -> bool:
        pid = self._pid(handle)
        if pid is None:
            return False
        try:
            # Signal 0 checks existence and permission without delivering
            # anything.
            os.kill(pid, 0)
        except ProcessLookupError, PermissionError:
            return False
        # A pid that has exited but not been reaped is still signalable, so
        # confirm against /proc before calling it alive.
        return _is_live_process(pid)


def _is_live_process(pid: int) -> bool:
    """Whether a pid is a process rather than an unreaped zombie."""
    status = Path(f"/proc/{pid}/stat")
    try:
        fields = status.read_text().rsplit(")", 1)[-1].split()
    except OSError:
        # No procfs, or it went away between the signal and this read.
        return True
    return bool(fields) and fields[0] != "Z"


@dataclass(frozen=True)
class UnknownExecutor:
    """No implementation on this machine answers to that name."""

    name: str
    available: tuple[str, ...]

    @property
    def message(self) -> str:
        known = ", ".join(self.available) or "none"
        return f"No executor named {self.name!r}. Available: {known}."


_EXECUTORS = {
    SYSTEMD_EXECUTOR: SystemdUnitExecutor,
    DEFAULT_EXECUTOR: LocalProcessExecutor,
}


def available_executors() -> tuple[str, ...]:
    return tuple(sorted(_EXECUTORS))


def get_executor(name: str | None = None) -> Executor | UnknownExecutor:
    """The executor for a name, falling back to what this machine detected.

    `None` means "whatever suits this machine" — a systemd unit on the server,
    a child process in a container — which is what starting a new run wants.
    Reading it back from a row passes the recorded name instead, so a run that
    ran as a unit is still cancelled as one after the default changes.
    """
    resolved = (name or default_executor()).strip()
    implementation = _EXECUTORS.get(resolved)
    if implementation is None:
        logger.warning("No executor named %r.", resolved)
        return UnknownExecutor(resolved, available_executors())
    return implementation()
