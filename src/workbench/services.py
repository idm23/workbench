"""What this instance of Workbench is actually running right now.

A second install shares this machine — production and staging are two
checkouts, two service accounts, two sets of systemd units — so "what's
running" has to mean *this instance's* units specifically, not everything
`systemctl` knows about. `config.service_name()`/`deploy_unit_name()` are
already instance-scoped for exactly that reason (see their own docstrings),
so this reads through them rather than pattern-matching unit names itself.

A currently-executing shell command is not a systemd concept at all — it is
one Bash tool call an active run has not yet gotten a result for. Read
straight out of `run_events`, the same table a run's own page streams from,
rather than a self-report the agent has to remember to make: an ordinary
Bash call finishes in seconds, so this is inherently a live snapshot rather
than a durable record, and `run_events` already has everything needed to
say "still going" without a second mechanism to keep in sync.
"""

import subprocess
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.config import deploy_unit_name, service_name, systemd_available
from workbench.database.models import Run, RunEvent, RunEventKind
from workbench.runs.executors import SYSTEMD_EXECUTOR
from workbench.runs.lifecycle import active_runs

#: Long enough for systemd to answer over D-Bus, short enough that a page
#: load never hangs on a wedged manager.
SYSTEMCTL_TIMEOUT_SECONDS = 10

#: How far back to look for an unresolved Bash call. A tool result almost
#: always follows within a few events of its call, so this is generous
#: rather than tuned — the cost of looking too far back is a few extra rows
#: read on a page nobody opens per second.
RECENT_EVENTS_SCANNED = 50


def _label_for(run: Run) -> str:
    """What to call a run on this page — the work, not the row id."""
    if run.task is not None:
        return f"Task: {run.task.title}"
    if run.project is not None:
        return f"Conversation: {run.project.owner}/{run.project.repo}"
    return f"Run {run.id}"


@dataclass(frozen=True)
class ServiceUnit:
    """One systemd unit belonging to this instance, and what it is doing."""

    unit: str
    label: str
    active: bool
    state: str
    #: Set only for a unit backing a run — the static app/deploy units have
    #: no one run to point at.
    run: Run | None = None

    @property
    def status_class(self) -> str:
        if self.active:
            return "active"
        if self.state.startswith("failed"):
            return "failed"
        return ""


def _show(units: list[str]) -> dict[str, tuple[bool, str]]:
    """ActiveState/SubState for each unit, in one call.

    A unit `systemctl` has never heard of — a run whose row is stale, a
    template not yet installed — answers `inactive`/`dead` rather than
    erroring, which is `systemctl show`'s own behaviour for an unknown unit
    and not something this has to special-case.
    """
    if not units:
        return {}
    result = subprocess.run(
        ["systemctl", "show", *units, "--property=Id,ActiveState,SubState"],
        capture_output=True,
        text=True,
        timeout=SYSTEMCTL_TIMEOUT_SECONDS,
        check=False,
    )
    statuses: dict[str, tuple[bool, str]] = {}
    for block in result.stdout.strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit = props.get("Id")
        if not unit:
            continue
        active_state = props.get("ActiveState", "unknown")
        sub_state = props.get("SubState", "")
        state = f"{active_state} ({sub_state})" if sub_state else active_state
        statuses[unit] = (active_state == "active", state)
    return statuses


def running_services(db: Session) -> list[ServiceUnit]:
    """Every unit this instance owns, plus one entry per run holding a
    concurrency slot right now — whichever executor actually started it.

    The static units (the app, the deploy timer) are skipped entirely on a
    machine with no systemd rather than shown as unknown: a laptop checkout
    was never going to have them, and saying so once via `systemd_available`
    in the template beats repeating "unknown" on every row.
    """
    runs = active_runs(db)
    services: list[ServiceUnit] = []

    if systemd_available():
        app_unit = f"{service_name()}.service"
        deploy_service = f"{deploy_unit_name()}.service"
        deploy_timer = f"{deploy_unit_name()}.timer"
        # Paired with its handle right where the None-check happens — a
        # `Run` alone still has an `str | None` handle, and pairing here is
        # what lets everything below use a plain `str`.
        systemd_runs = [(r, r.handle) for r in runs if r.executor == SYSTEMD_EXECUTOR and r.handle]
        statuses = _show([app_unit, deploy_service, deploy_timer, *(h for _, h in systemd_runs)])

        for unit, label in (
            (app_unit, "Web app"),
            (deploy_service, "Deploy (last check)"),
            (deploy_timer, "Deploy timer"),
        ):
            active, state = statuses.get(unit, (False, "unknown"))
            services.append(ServiceUnit(unit=unit, label=label, active=active, state=state))

        for run, handle in systemd_runs:
            active, state = statuses.get(handle, (False, "unknown"))
            services.append(
                ServiceUnit(unit=handle, label=_label_for(run), active=active, state=state, run=run)
            )

    for run in runs:
        if run.executor == SYSTEMD_EXECUTOR and run.handle:
            continue  # already added above, with its real unit status
        services.append(
            ServiceUnit(
                unit=run.handle or f"run-{run.id}",
                label=_label_for(run),
                active=True,
                state=run.executor or "local process",
                run=run,
            )
        )
    return services


@dataclass(frozen=True)
class ActiveShell:
    """A Bash command an in-flight run has started and not yet gotten a
    result for."""

    run: Run
    command: str

    @property
    def label(self) -> str:
        return _label_for(self.run)


def active_shells(db: Session) -> list[ActiveShell]:
    """The one currently-unresolved Bash call per active run, if it has one.

    A snapshot, deliberately not a log: an ordinary command finishes in
    seconds, so this recomputes from `run_events` on every page load rather
    than persisting anything of its own.
    """
    shells: list[ActiveShell] = []
    for run in active_runs(db):
        rows = db.execute(
            select(RunEvent.kind, RunEvent.payload)
            .where(RunEvent.run_id == run.id)
            .order_by(RunEvent.seq.desc())
            .limit(RECENT_EVENTS_SCANNED)
        ).all()

        resolved = {
            payload["id"]
            for kind, payload in rows
            if kind is RunEventKind.TOOL_RESULT and payload.get("id")
        }
        for kind, payload in rows:
            if kind is not RunEventKind.TOOL_USE or payload.get("name") != "Bash":
                continue
            if payload.get("id") in resolved:
                continue
            command = (payload.get("input") or {}).get("command", "")
            shells.append(ActiveShell(run=run, command=command))
            break  # newest unresolved call only — that is the one still going

    return shells
