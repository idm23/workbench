"""What Workbench asks of an agent, and what it accepts back.

This module is the seam. Everything above it — the runner, the API, the
templates — is written against these types, and nothing here knows that any
particular vendor's SDK exists. A backend is anything that can take an
`AgentRequest` and yield `AgentEvent`s in Workbench's own vocabulary, ending
with exactly one outcome.

Events and the outcome travel in one stream rather than through a callback or
a second return channel. A backend is a process being read to exhaustion, and
one iteration is the only shape that cannot get out of order: a caller that
has seen the outcome has, by construction, already seen every event before it.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from workbench.database.models import RunEventKind, RunPhase


@dataclass(frozen=True)
class AgentEvent:
    """One thing that happened, ready to be persisted and streamed.

    `kind` is a `RunEventKind` — Workbench's vocabulary, not a passthrough of
    whatever the backend emitted. Translating here rather than at the reader is
    what keeps a run recorded a year ago legible after a backend switch.

    `payload` must be JSON-serialisable: it is stored in a JSON column and
    replayed to the browser verbatim.
    """

    kind: RunEventKind
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRequest:
    """One attempt at one phase of one task.

    `prompt` is built by Workbench rather than by the backend — see
    `workbench.agents.prompts`. `phase` is passed alongside it so a backend
    that can *enforce* the plan phase does, rather than merely asking for it.
    """

    #: The task's worktree. The agent's working directory, and the only place
    #: it is expected to write.
    worktree: Path

    phase: RunPhase
    prompt: str

    #: An opaque handle from an earlier run on this task, so the execute phase
    #: continues the planning conversation instead of starting cold. Never
    #: parsed, and meaningless to a backend other than the one that issued it —
    #: which is why callers read it together with the run's `backend`.
    resume_token: str | None = None

    #: A preference, not a guarantee. What actually ran comes back on the
    #: outcome, because that is what gets recorded.
    model: str | None = None


@dataclass(frozen=True)
class AgentFinished:
    """The agent ran to completion and had the last word.

    `text` is the phase's product: the plan for a plan run, the summary for an
    execute run. Written by the agent while it still has the context, which is
    why Workbench has no separate summariser.
    """

    text: str
    resume_token: str | None = None
    model: str | None = None
    total_cost_usd: float | None = None
    num_turns: int | None = None


@dataclass(frozen=True)
class AgentFailed:
    """The agent started and then failed.

    Distinct from `AgentUnavailable` because work may already have happened:
    there can be commits, a dirty worktree, and a cost to record. Usage fields
    carry whatever was known at the point of failure.
    """

    message: str
    resume_token: str | None = None
    model: str | None = None
    total_cost_usd: float | None = None
    num_turns: int | None = None


@dataclass(frozen=True)
class AgentUnavailable:
    """The agent could not be started at all.

    Nothing was attempted, so there is nothing to summarise, no diff to take,
    and no cost. A missing CLI, an unusable credential, or a backend name this
    machine has no implementation for all land here.
    """

    message: str


#: Exactly one of these is yielded, and it is always the last thing yielded.
type AgentOutcome = AgentFinished | AgentFailed | AgentUnavailable

#: What a backend yields: any number of events, then one outcome.
type AgentStream = AsyncIterator[AgentEvent | AgentOutcome]


@runtime_checkable
class Backend(Protocol):
    """An agent Workbench can drive.

    Implementations live in this package, one module each, and are reached
    through `workbench.agents.registry.get_backend`. A backend's module is the
    only place its SDK may be imported — that constraint is what the swappable
    backend decision actually amounts to, and `tests/test_seam.py` enforces it.
    """

    @property
    def name(self) -> str:
        """The identifier stored in `runs.backend`. Matches the registry key."""
        ...

    def run(self, request: AgentRequest) -> AgentStream:
        """Drive one attempt, yielding events as they arrive.

        Must not raise for an ordinary failure — a missing CLI, a refused
        credential, a crashed subprocess — and must not touch the database.
        Persistence belongs to the runner, so that a backend can be exercised
        in a test with neither a schema nor a model behind it.
        """
        ...
