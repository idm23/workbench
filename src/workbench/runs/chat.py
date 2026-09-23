"""The project's chat: every conversation session it has had, read as one thread.

A project conversation is a run, and a run ends when nobody has typed for a
while (`config.input_idle_seconds`). The next message starts another run that
resumes the same session, so to the agent it is one conversation. This is what
makes it read as one to a person too: the page beside the task tree shows the
chat parts of all those runs in order, with a marker where a new session began.

Only the parts of a run a person reads as conversation are kept. Thinking and
tool results belong on the run's own page, which the panel links to. Tool calls
stay as one muted line each, because "it went and looked at the tests" is part
of the answer.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.database.models import Run, RunEvent, RunEventKind, RunPhase, RunStatus

#: What the chat shows of a run. Everything else is on the run's page.
CHAT_KINDS = (RunEventKind.INPUT, RunEventKind.TEXT, RunEventKind.TOOL_USE)

#: How much of the thread the page renders. Older messages are on each run's
#: page; a chat that rendered months of history would be a slow page.
CHAT_HISTORY_LIMIT = 200


@dataclass(frozen=True)
class ChatMessage:
    run_id: int
    seq: int
    kind: str
    text: str


@dataclass(frozen=True)
class SessionStart:
    """Where one run of the conversation began."""

    run_id: int
    started_at: datetime


type ChatEntry = ChatMessage | SessionStart


def chat_history(db: Session, project_id: int, limit: int = CHAT_HISTORY_LIMIT) -> list[ChatEntry]:
    """The newest `limit` chat messages of this project, oldest first, with
    a `SessionStart` before each run's first message."""
    newest_first = db.execute(
        select(RunEvent.run_id, RunEvent.seq, RunEvent.kind, RunEvent.payload)
        .join(Run, Run.id == RunEvent.run_id)
        .where(
            Run.project_id == project_id,
            Run.phase == RunPhase.CONVERSATION,
            RunEvent.kind.in_(CHAT_KINDS),
        )
        .order_by(RunEvent.run_id.desc(), RunEvent.seq.desc())
        .limit(limit)
    ).all()
    rows = list(reversed(newest_first))

    runs = {
        run.id: run
        for run in db.scalars(select(Run).where(Run.id.in_({row.run_id for row in rows})))
    }

    entries: list[ChatEntry] = []
    current: int | None = None
    for run_id, seq, kind, payload in rows:
        if run_id != current:
            entries.append(SessionStart(run_id, runs[run_id].created_at))
            current = run_id
        payload = payload or {}
        text = payload.get("name") if kind is RunEventKind.TOOL_USE else payload.get("text")
        entries.append(ChatMessage(run_id, seq, str(kind), text or ""))
    return entries


def failed_without_a_word(db: Session, project_id: int) -> Run | None:
    """The newest conversation run, if it failed before saying anything.

    A session that dies at authentication writes no chat message at all, so
    `chat_history` would never show it. Someone who typed and got nothing
    back needs to see why.
    """
    run = db.scalars(
        select(Run)
        .where(Run.project_id == project_id, Run.phase == RunPhase.CONVERSATION)
        .order_by(Run.id.desc())
        .limit(1)
    ).first()
    if run is None or run.status is not RunStatus.FAILED:
        return None
    said = db.scalar(
        select(RunEvent.id)
        .where(RunEvent.run_id == run.id, RunEvent.kind == RunEventKind.TEXT)
        .limit(1)
    )
    return run if said is None else None
