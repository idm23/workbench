"""Database schema.

A user owns projects, a project holds a tree of tasks, and a task accumulates
runs — one row per attempt to work it. Runs stream their output into
`run_events`, which is what makes an agent's progress survive a restart.
"""

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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


# Stored as plain strings rather than a SQL enum: SQLite has no native enum, so
# Alembic would emit a CHECK constraint that then needs a table rewrite every
# time a state is added. These are young and will gain states.
TaskStatus = Literal["open", "active", "blocked", "done", "cancelled"]

# `awaiting_review` is the one that earns its keep: a plan run stops there and
# waits for a human, which is the whole point of the plan/execute split.
RunStatus = Literal[
    "queued",
    "running",
    "awaiting_review",
    "succeeded",
    "failed",
    "cancelled",
]

RunPhase = Literal["plan", "execute"]

#: Runs in these states are finished; nothing further will happen to them.
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


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

    # Where the repository is cloned on this machine. Null until cloned, and
    # nothing can run against the project until it is set: `git worktree add`
    # needs a real checkout to hang worktrees off.
    local_path: Mapped[str | None] = mapped_column(String(1000), default=None)

    # Run inside a newly created worktree. `git worktree add` gives tracked
    # files only — no .env, no node_modules, no venv — so most first builds in a
    # fresh worktree fail for reasons unrelated to the task.
    setup_command: Mapped[str | None] = mapped_column(Text, default=None)

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
    status: Mapped[str] = mapped_column(String(20), default="open")

    # Explicit ordering among siblings, so reordering later does not depend on
    # created_at and does not renumber the whole tree.
    position: Mapped[int] = mapped_column(Integer, default=0)

    # On the task rather than the run, deliberately. A plan run and the execute
    # run that follows it share one worktree, because resuming the planning
    # session requires the same working directory it ran in.
    branch: Mapped[str | None] = mapped_column(String(300), default=None)
    worktree_path: Mapped[str | None] = mapped_column(String(1000), default=None)

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

    phase: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)

    # The SDK's session, kept so the execute phase can resume the conversation
    # that produced the plan rather than starting cold.
    session_id: Mapped[str | None] = mapped_column(String(100), default=None)

    # The detached runner process. Used to cancel a run, and to notice one that
    # died without recording an outcome.
    pid: Mapped[int | None] = mapped_column(Integer, default=None)

    plan: Mapped[str | None] = mapped_column(Text, default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    diffstat: Mapped[str | None] = mapped_column(Text, default=None)
    pr_url: Mapped[str | None] = mapped_column(String(500), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    # Reported by the SDK on completion. Recorded now because backfilling cost
    # after the fact is impossible.
    total_cost_usd: Mapped[float | None] = mapped_column(Float, default=None)
    num_turns: Mapped[int | None] = mapped_column(Integer, default=None)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
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
        UniqueConstraint("run_id", "seq", name="uq_run_events_run_id_seq"),
        # Replay is always "everything after N for this run", so the index has
        # to cover both columns in that order to be useful.
        Index("ix_run_events_run_id_seq", "run_id", "seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))

    #: Monotonic per run, starting at 1. Doubles as the SSE event id.
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))

    #: JSON, as text. SQLite would store it as text regardless, and keeping the
    #: encoding explicit means the reader never guesses.
    payload: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run: Mapped[Run] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return f"<RunEvent run={self.run_id} seq={self.seq} {self.kind}>"
