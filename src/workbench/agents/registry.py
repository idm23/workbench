"""Resolving a backend name to something that can run.

`runs.backend` and `projects.agent_backend` hold a name — a plain string, so
that adding a backend never needs a migration. This is the one place a name
becomes an implementation.

The mapping holds *factories*, not instances, and each factory imports its
module inside the call. That is the point rather than an optimisation: the web
process resolves backend names constantly — rendering a run, validating a
project's override — and must never pull an agent SDK into its import graph to
do it. Only the detached runner actually constructs one.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from workbench.agents.protocol import Backend
from workbench.config import default_agent_backend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnknownBackend:
    """No implementation on this machine answers to that name.

    Carries the names that do, because the realistic cause is a typo in
    `WORKBENCH_AGENT_BACKEND` or in a project's override, and a message that
    lists the alternatives ends the investigation there.
    """

    name: str
    available: tuple[str, ...]

    @property
    def message(self) -> str:
        known = ", ".join(self.available) or "none"
        return f"No agent backend named {self.name!r}. Available: {known}."


def _claude() -> Backend:
    # Imported here, not at module scope: this is the only import of an agent
    # SDK in the codebase, and it stays out of the web process.
    from workbench.agents.claude import ClaudeBackend

    return ClaudeBackend()


#: Name to factory. A module-level constant rather than a registry something
#: mutates at import time — a backend that only exists once some other module
#: has been imported is a backend that works in one process and not another.
_FACTORIES: dict[str, Callable[[], Backend]] = {
    "claude": _claude,
}


def available_backends() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def get_backend(name: str | None = None) -> Backend | UnknownBackend:
    """The backend for a name, falling back to this machine's default.

    `None` means "whatever this machine is configured for", which is what a
    project with no `agent_backend` override wants. Resolving that here rather
    than at each call site keeps the precedence — project, then machine — in
    one place.
    """
    resolved = (name or default_agent_backend()).strip()
    factory = _FACTORIES.get(resolved)
    if factory is None:
        logger.warning("No agent backend named %r.", resolved)
        return UnknownBackend(resolved, available_backends())
    return factory()
