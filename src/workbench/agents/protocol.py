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

    #: Plain identifiers, not a vendor shape — a backend that can call back
    #: into Workbench's own API (the live outcome report, a subtask spun off
    #: mid-execute) needs to say which run and task it is. Never parsed by
    #: anything above the seam; only a backend turns them into whatever a
    #: running agent can actually see (an environment variable, say).
    run_id: int = 0
    task_id: int = 0

    #: Set instead of `task_id` for a project conversation — never both.
    #: Zero, like `task_id`, when it does not apply.
    project_id: int = 0

    #: Messages typed into this run after it started, delivered lazily as
    #: they arrive rather than handed over all at once — the whole point is
    #: that they can keep coming while the backend is already working.
    #: `None` for a request nothing wired this up for, which behaves exactly
    #: as if no one ever types anything: `prompt` alone, unchanged.
    inputs: AsyncIterator[str] | None = None


@dataclass(frozen=True)
class SubtaskProposal:
    """One piece of a plan's proposed decomposition.

    `ready_to_execute` is the plan's own judgement that this piece is fully
    specified — the resulting task's `entry_phase` starts at execute rather
    than plan when true, skipping a planning pass the plan itself already did.
    """

    title: str
    body: str
    ready_to_execute: bool = False


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

    #: A plan run's proposed subtasks, from structured output. Always `None`
    #: for an execute run — decomposition is a plan-phase concept.
    proposed_subtasks: list[SubtaskProposal] | None = None

    #: True when the backend cut the conversation short itself — hitting the
    #: turn limit, say — rather than the agent choosing to stop. Distinct from
    #: whether the process crashed: this is still `AgentFinished`, just not
    #: trustworthy as a genuine "I'm done", which is why a self-reported
    #: `finished` outcome is distrusted when this is true.
    stopped_early: bool = False


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


#: The vocabulary for `CredentialStatus.method`. Workbench's own words, not a
#: passthrough of whatever a vendor calls its login: a second backend spelling
#: the same two states differently is what makes a shared reader impossible.
#:
#: `unknown` is distinct from `none` deliberately. "Nobody has logged in" is a
#: problem to report loudly; "the probe could not run" is not, because a
#: warning that fires when the checker itself breaks is one people learn to
#: ignore, and then miss the real one.
CREDENTIAL_SUBSCRIPTION = "subscription"
CREDENTIAL_API_KEY = "api_key"
CREDENTIAL_NONE = "none"
CREDENTIAL_UNKNOWN = "unknown"


@dataclass(frozen=True)
class CredentialStatus:
    """What credential this backend would use, and whose account pays.

    Deliberately not an outcome type. An outcome describes an attempt that was
    made; the entire point of this is to answer the question without making
    one — before a run exists, from an installer, and from a health check that
    must cost nothing.

    `method` is the load-bearing field, and `logged_in` on its own is not an
    assertion worth making. A stray `ANTHROPIC_API_KEY` anywhere in the
    environment makes a backend report itself perfectly authenticated while
    moving every run onto metered billing — the exact silent failure
    `config.billing_mode` exists to prevent. So a backend that finds one while
    Workbench is billing a subscription reports `logged_in=False` and says so
    in `detail`.
    """

    backend: str

    #: Whether a run started right now would authenticate the way Workbench
    #: intends. Not "is there a credential" — see the note above.
    logged_in: bool

    #: One of the `CREDENTIAL_*` constants above.
    method: str

    #: Whatever names the payer: an email, an organisation, an account id.
    #: None when nothing is authenticated, or when the probe could not say.
    account: str | None = None

    #: One line for a person. Read by the doctor and by the web banner, so it
    #: must be legible on its own, without the surrounding check's title.
    detail: str = ""

    #: The exact argv that would authenticate this backend, or empty if there
    #: is nothing a person could run. Carried here rather than composed by the
    #: caller because it is the one piece of the fix that is vendor-shaped: a
    #: doctor that spelled out `claude auth login` would be a second place
    #: that knows which vendor answered.
    login_command: tuple[str, ...] = ()


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

    def credential_status(self) -> CredentialStatus:
        """Whether this backend could run right now, and on whose account.

        Must not raise, for the same reason `run` must not: an absent or
        unusable credential is an ordinary condition to report, not an error
        to handle. A probe that cannot be made at all answers
        `CREDENTIAL_UNKNOWN` rather than guessing.

        Evaluated against `config.agent_environment()` rather than the raw
        environment, so it sees what the runner will see. Reading `os.environ`
        directly would report a credential the runner strips, which is worse
        than not checking: it would say "authenticated" about a variable that
        is removed before the agent ever starts.

        Asked about *the account running this process* — there is no argument
        for whose credential to check, because the answer lives in a home
        directory and the only honest way to ask about another account is to
        run as it. That is what the installer does.

        Reached from `python -m workbench.doctor`. The web process learns the
        answer over a process boundary and never by constructing a backend;
        `tests/test_seam.py` asserts that in both directions.
        """
        ...
