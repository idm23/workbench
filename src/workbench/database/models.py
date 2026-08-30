"""Database schema.

A user owns projects, a project holds a tree of tasks, and a task accumulates
runs — one row per attempt to work it. Runs stream their output into
`run_events`, which is what makes an agent's progress survive a restart.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from workbench.config import DEFAULT_BACKEND, DEFAULT_EXECUTOR

# Explicit constraint naming matters more than usual here: SQLite cannot ALTER
# most constraints, so Alembic rewrites the table instead (batch mode), and it
# can only do that for constraints it can name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# StrEnum rather than bare strings or a Literal alias. Members compare equal to
# their own values, so `run.status == "running"` still works and templates
# render them unchanged — but there is now one place the states are spelled,
# and a typo is a type error instead of a condition that silently never fires.
#
# The `Enum(..., native_enum=False)` column below stores the value, not the
# member name, so the database holds the same lowercase strings either way.


class TaskStatus(StrEnum):
    OPEN = "open"
    ACTIVE = "active"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class RunPhase(StrEnum):
    """Workbench's own two-step workflow, not a property of any backend.

    A backend with no read-only mode of its own can honour the plan phase by
    instruction rather than enforcement; the phases are what Workbench asks
    for, and adapting to them is the adapter's job.
    """

    PLAN = "plan"
    EXECUTE = "execute"


class RunEventKind(StrEnum):
    """The vocabulary a run's output is recorded in.

    Deliberately Workbench's own rather than whatever an SDK happens to emit.
    A backend adapter translates into these, so the event log, the SSE stream,
    and the templates keep working when the backend behind them changes, and a
    run recorded a year ago stays readable after a switch.

    Anything a backend emits with no equivalent here belongs in NOTICE rather
    than in a new member — the set only grows when the *reader* needs the
    distinction.
    """

    #: Prose from the agent, meant to be read.
    TEXT = "text"
    #: Reasoning, where the backend exposes it separately from prose.
    THINKING = "thinking"
    #: The agent invoked a tool.
    TOOL_USE = "tool_use"
    #: What that tool returned.
    TOOL_RESULT = "tool_result"
    #: A lifecycle change Workbench itself recorded, not the backend.
    STATUS = "status"
    #: Anything else worth showing: backend chatter, warnings, progress.
    NOTICE = "notice"
    #: A message a person typed into a run while it was still going. Its own
    #: kind rather than folded into NOTICE: a reader needs to tell what the
    #: person said apart from what the agent or the backend said.
    INPUT = "input"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    # Earns its keep: a plan run stops here and waits for a person, which is
    # the whole point of the plan/execute split.
    AWAITING_REVIEW = "awaiting_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Nothing further will happen to a run in this state.

        `awaiting_review` is deliberately not terminal — the run is paused,
        and approving it starts the next phase.
        """
        return self in _TERMINAL_RUN_STATUSES

    @property
    def is_active(self) -> bool:
        """The run occupies a slot against the concurrency limit."""
        return self in _ACTIVE_RUN_STATUSES


_TERMINAL_RUN_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})
_ACTIVE_RUN_STATUSES = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})


class RunOutcome(StrEnum):
    """What the agent itself reported through the live outcome API.

    Distinct from `RunStatus`: a backend process can exit cleanly
    (`AgentFinished`) while the agent reports that the task actually needs
    re-planning or failed outright, and `record()` maps the two together
    rather than treating "the process didn't crash" as "it succeeded".
    """

    FINISHED = "finished"
    FAILED = "failed"
    NEEDS_REPLANNING = "needs_replanning"


def _stored_values(enum_type: type[StrEnum]) -> list[str]:
    """Persist enum *values*, not member names.

    SQLAlchemy's Enum stores `RUNNING` by default. Storing `running` keeps the
    column readable in a SQLite browser and means the rendered HTML, the CSS
    class names, and the database all agree.
    """
    return [member.value for member in enum_type]


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    projects: Mapped[list[Project]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="Project.created_at",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} name={self.name!r}>"


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("user_id", "github_url", name="uq_projects_user_github_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    # owner/repo are stored parsed rather than re-derived from the URL at each
    # call site, since every GitHub API call needs them separately.
    owner: Mapped[str] = mapped_column(String(100))
    repo: Mapped[str] = mapped_column(String(100))
    github_url: Mapped[str] = mapped_column(String(500))

    # Both nullable: populated from the GitHub API when it answers, left empty
    # when it rate-limits or the network is down, rather than failing the add.
    description: Mapped[str | None] = mapped_column(Text, default=None)
    default_branch: Mapped[str | None] = mapped_column(String(100), default=None)

    # Run inside a newly created worktree. `git worktree add` gives tracked
    # files only — no .env, no node_modules, no venv — so most first builds in a
    # fresh worktree fail for reasons unrelated to the task.
    setup_command: Mapped[str | None] = mapped_column(Text, default=None)

    # Which agent to use for this project's tasks. Null means the configured
    # default, so choosing a backend is per project rather than per machine,
    # and changing it here only affects runs started afterwards — existing runs
    # keep the backend recorded on them.
    agent_backend: Mapped[str | None] = mapped_column(String(50), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship(back_populates="projects")
    tasks: Mapped[list[Task]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Project id={self.id} {self.owner}/{self.repo}>"


class Task(Base):
    """A unit of work, which may contain further units of work.

    The tree is a self-referencing parent_id rather than a nested set or
    materialised path. These trees are small and read whole, so the simplest
    representation is the right one.
    """

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True, default=None
    )

    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(
            TaskStatus,
            # SQLite has no native enum type, so this is a VARCHAR. No CHECK
            # constraint either: adding a state would otherwise need a table
            # rewrite, and these are young enough to keep gaining states.
            native_enum=False,
            create_constraint=False,
            length=20,
            values_callable=_stored_values,
        ),
        default=TaskStatus.OPEN,
    )

    # Explicit ordering among siblings, so reordering later does not depend on
    # created_at and does not renumber the whole tree.
    position: Mapped[int] = mapped_column(Integer, default=0)

    # On the task rather than the run, deliberately. A plan run and the execute
    # run that follows it share one worktree, because resuming the planning
    # session requires the same working directory it ran in.
    branch: Mapped[str | None] = mapped_column(String(300), default=None)
    worktree_path: Mapped[str | None] = mapped_column(String(1000), default=None)

    # What the task's branch should be created from: "main", "staging", or
    # another task's own branch (see workbench.tasks.origin). Unset until
    # someone chooses one when starting the first run — there is no sane
    # default to assume on a task's behalf, because the whole point is that
    # the choice is explicit rather than whatever a stale clone happens to
    # have on hand. Once the worktree exists this is historical: the branch
    # it names is already fixed and nothing re-reads this to change it.
    origin_ref: Mapped[str | None] = mapped_column(String(300), default=None)

    # What phase this task's *first* run should be. Unset means today's only
    # behaviour, plan first. Set to execute when a plan's decomposition
    # judged a subtask already fully specified — see workbench.tasks.store.
    entry_phase: Mapped[RunPhase | None] = mapped_column(
        Enum(
            RunPhase,
            native_enum=False,
            create_constraint=False,
            length=20,
            values_callable=_stored_values,
        ),
        default=None,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    project: Mapped[Project] = relationship(back_populates="tasks")
    children: Mapped[list[Task]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by="Task.position, Task.id",
    )
    parent: Mapped[Task | None] = relationship(
        back_populates="children",
        remote_side="Task.id",
    )
    runs: Mapped[list[Run]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Run.id",
    )

    def __repr__(self) -> str:
        return f"<Task id={self.id} {self.status} {self.title!r}>"


class Run(Base):
    """One attempt at a task: either planning it or carrying the plan out."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)

    phase: Mapped[RunPhase] = mapped_column(
        Enum(
            RunPhase,
            native_enum=False,
            create_constraint=False,
            length=20,
            values_callable=_stored_values,
        )
    )
    status: Mapped[RunStatus] = mapped_column(
        Enum(
            RunStatus,
            native_enum=False,
            create_constraint=False,
            length=20,
            values_callable=_stored_values,
        ),
        default=RunStatus.QUEUED,
        # Indexed because the concurrency check counts active runs across every
        # project on each attempt to start one.
        index=True,
    )

    # Which agent actually ran this. A plain string rather than an enum: the
    # set of backends is open, and someone adding their own should not have to
    # write a migration to do it.
    #
    # Recorded per run, not just configured globally, because the moment a
    # second backend exists every historical row becomes ambiguous without it —
    # and unlike most columns this one cannot be backfilled from anything.
    backend: Mapped[str] = mapped_column(String(50), default=DEFAULT_BACKEND)

    # What that backend was actually pointed at. Null when the backend does not
    # name its model, or does not have one.
    model: Mapped[str | None] = mapped_column(String(100), default=None)

    # An opaque handle for continuing this conversation, so the execute phase
    # resumes the plan rather than starting cold.
    #
    # Deliberately not named for any one backend's concept of a session, and
    # deliberately never parsed: it means nothing except to the backend that
    # issued it, and nothing at all to a different one. Always read it together
    # with `backend` above.
    resume_token: Mapped[str | None] = mapped_column(String(200), default=None)

    # How this run was started, and what to ask about it afterwards.
    #
    # Recorded per run for the same reason `backend` is: the moment a second
    # executor exists — a systemd unit here, a job on a GPU node later — every
    # earlier row is ambiguous without it, and nothing can backfill where a run
    # actually ran.
    executor: Mapped[str] = mapped_column(String(50), default=DEFAULT_EXECUTOR)

    # An opaque reference to the running job, meaningful only to the executor
    # that issued it: a unit name for systemd, a pid for a bare process, a
    # remote job id later. Never parsed, and always read together with
    # `executor` above — exactly like `resume_token` and `backend`.
    #
    # Set by whatever starts the run rather than by the runner itself, so there
    # is no window where a run is executing and nothing knows how to stop it.
    handle: Mapped[str | None] = mapped_column(String(200), default=None)

    plan: Mapped[str | None] = mapped_column(Text, default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    diffstat: Mapped[str | None] = mapped_column(Text, default=None)
    pr_url: Mapped[str | None] = mapped_column(String(500), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    # What the agent itself reported happened, via the live outcome API —
    # distinct from whether the backend process merely exited without
    # crashing. Read once, by runs.runner.record(). Null means the agent
    # never called it.
    agent_outcome: Mapped[RunOutcome | None] = mapped_column(
        Enum(
            RunOutcome,
            native_enum=False,
            create_constraint=False,
            length=20,
            values_callable=_stored_values,
        ),
        default=None,
    )

    # The agent's own one-line explanation for `agent_outcome`, for
    # `failed`/`needs_replanning`. Kept apart from `error` (reserved for an
    # actual crash) because `needs_replanning` is an expected, unalarming
    # outcome and must not render with `error`'s red styling.
    outcome_detail: Mapped[str | None] = mapped_column(Text, default=None)

    # A plan run's proposed decomposition, from structured output:
    # {"subtasks": [{"title", "body", "ready_to_execute"}, ...]}. Consumed
    # once, by the approve route, then left as a record of what was proposed.
    proposed_subtasks: Mapped[dict | None] = mapped_column(JSON, default=None)

    # Reported by the backend on completion, where it reports them at all — a
    # locally hosted model will leave cost null rather than zero. Recorded now
    # because backfilling cost
    # after the fact is impossible.
    total_cost_usd: Mapped[float | None] = mapped_column(Float, default=None)
    num_turns: Mapped[int | None] = mapped_column(Integer, default=None)

    # created_at, not started_at: the row exists while the run is still queued,
    # so this timestamps the request rather than the work. Nothing needs the
    # gap between them — the queue is a concurrency gate, not a backlog.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    task: Mapped[Task] = relationship(back_populates="runs")
    events: Mapped[list[RunEvent]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="RunEvent.seq",
    )

    def __repr__(self) -> str:
        return f"<Run id={self.id} {self.phase} {self.status}>"


class RunEvent(Base):
    """One message from a run, persisted as it arrives.

    This table is why a run survives a restart of the web process and why a
    phone that sleeps mid-run loses nothing: the SSE endpoint replays from the
    client's last sequence number before tailing, so the stream is recoverable
    rather than fire-and-forget.
    """

    __tablename__ = "run_events"
    __table_args__ = (
        # Replay is always "everything after sequence N, for this run", so this
        # constraint's own index is exactly the one those queries need — a
        # separate index on the same two columns would be dead weight on the
        # hottest write path in the app.
        UniqueConstraint("run_id", "seq", name="uq_run_events_run_id_seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))

    #: Monotonic per run, starting at 1. Doubles as the SSE event id.
    seq: Mapped[int] = mapped_column(Integer)

    #: Constrained to Workbench's own vocabulary rather than left free-form, so
    #: two backends cannot end up writing two spellings of the same thing and
    #: the templates keep rendering old runs after a switch.
    kind: Mapped[RunEventKind] = mapped_column(
        Enum(
            RunEventKind,
            native_enum=False,
            create_constraint=False,
            length=40,
            values_callable=_stored_values,
        )
    )

    #: The event's contents, shape depending on `kind`.
    #:
    #: Typed as JSON so SQLAlchemy owns the encoding. It is still TEXT in
    #: SQLite, but the writer no longer calls json.dumps and the reader no
    #: longer has to guard against a json.JSONDecodeError on its own rows —
    #: two places the round trip could previously disagree.
    payload: Mapped[dict] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run: Mapped[Run] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<RunEvent run={self.run_id} seq={self.seq} {self.kind}>"


class RunInput(Base):
    """A message typed into a run while it is still going.

    Deliberately its own table rather than reusing `run_events`, even though
    it mirrors it exactly: this one is polled by the runner as a queue of
    work to deliver to the backend, and `run_events` is a display log — a
    sent message is written to both, but only this one is ever read back out
    by anything other than a person's browser.
    """

    __tablename__ = "run_inputs"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_inputs_run_id_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))

    #: Monotonic per run, starting at 1 — same role as `RunEvent.seq`: what
    #: the runner's poll resumes from.
    seq: Mapped[int] = mapped_column(Integer)

    body: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    def __repr__(self) -> str:
        return f"<RunInput run={self.run_id} seq={self.seq}>"
