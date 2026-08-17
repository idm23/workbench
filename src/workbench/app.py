"""The web application: users, their projects, and the forms to add both.

Route handlers are sync `def` rather than `async def` deliberately. FastAPI runs
sync handlers in a threadpool, so the blocking SQLAlchemy and httpx calls here
cannot stall the event loop. When streaming agent output arrives it will be one
`async def` endpoint reading a durable log, not a rewrite of these.

Messages are passed between requests as query parameters rather than flash
state, which keeps the app free of session middleware and a signing secret.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from workbench.db import get_session_factory
from workbench.github import (
    InvalidReference,
    RepoMetadata,
    RepoNotFound,
    credentials_missing,
    fetch_repo_metadata,
    parse_repo_reference,
)
from workbench.models import TERMINAL_RUN_STATUSES, Project, Run, RunEvent, Task, User
from workbench.runs import RunRefused, cancel_run, reap_stale_runs, start_run
from workbench.tasks import build_tree, flatten
from workbench.worktrees import Cloned, clone_project, remove_worktree

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

app = FastAPI(title="Workbench")


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]


def _redirect(path: str, **messages: str | None) -> RedirectResponse:
    query = urlencode({key: value for key, value in messages.items() if value})
    target = f"{path}?{query}" if query else path
    # 303 so the browser follows up with a GET and a refresh does not re-post.
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def _get_user_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No user with id {user_id}.")
    return user


def _get_project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No project with id {project_id}.")
    return project


def _get_task_or_404(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No task with id {task_id}.")
    return task


def _get_run_or_404(db: Session, run_id: int) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No run with id {run_id}.")
    return run


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def list_users(request: Request, db: DbSession, error: str | None = None) -> HTMLResponse:
    users = db.scalars(
        # selectinload so rendering each user's project count is one extra
        # query rather than one per user.
        select(User).options(selectinload(User.projects)).order_by(User.name)
    ).all()
    return templates.TemplateResponse(request, "users.html", {"users": users, "error": error})


@app.post("/users")
def create_user(db: DbSession, name: Annotated[str, Form()]) -> RedirectResponse:
    cleaned = name.strip()
    if not cleaned:
        return _redirect("/", error="Enter a name.")

    db.add(User(name=cleaned))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _redirect("/", error=f"There is already a user called {cleaned}.")
    return _redirect("/")


@app.get("/users/{user_id}", response_class=HTMLResponse)
def show_user(
    request: Request,
    db: DbSession,
    user_id: int,
    error: str | None = None,
    notice: str | None = None,
) -> HTMLResponse:
    user = _get_user_or_404(db, user_id)
    return templates.TemplateResponse(
        request,
        "user_detail.html",
        {"user": user, "error": error, "notice": notice},
    )


@app.post("/users/{user_id}/projects")
def add_project(db: DbSession, user_id: int, reference: Annotated[str, Form()]) -> RedirectResponse:
    user = _get_user_or_404(db, user_id)
    target = f"/users/{user.id}"

    ref = parse_repo_reference(reference)
    if isinstance(ref, InvalidReference):
        return _redirect(target, error=ref.message)

    lookup = fetch_repo_metadata(ref)
    if isinstance(lookup, RepoNotFound):
        return _redirect(target, error=lookup.message)

    # Anything short of a definitive "not found" still saves. A blank
    # description is better than refusing the add because GitHub was busy.
    metadata: RepoMetadata | None = None
    notice: str | None = None
    if isinstance(lookup, RepoMetadata):
        metadata = lookup
    else:
        notice = f"Added {ref.slug} without details. {lookup.message}"

    db.add(
        Project(
            user_id=user.id,
            owner=ref.owner,
            repo=ref.repo,
            github_url=ref.url,
            description=metadata.description if metadata else None,
            default_branch=metadata.default_branch if metadata else None,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _redirect(target, error=f"{user.name} already has {ref.slug}.")

    return _redirect(target, notice=notice)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def show_project(
    request: Request,
    db: DbSession,
    project_id: int,
    error: str | None = None,
    notice: str | None = None,
) -> HTMLResponse:
    project = _get_project_or_404(db, project_id)

    # Correct any run whose process died without saying so, before rendering a
    # page that would otherwise show it as permanently in progress.
    reap_stale_runs(db)

    tasks = db.scalars(
        select(Task)
        .where(Task.project_id == project.id)
        .options(selectinload(Task.runs))
        .order_by(Task.position, Task.id)
    ).all()

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "project": project,
            "nodes": flatten(build_tree(list(tasks))),
            "error": error,
            "notice": notice,
            # Surfaced on the page rather than at run time, so the missing
            # piece is visible before an agent spends minutes on the work.
            "credentials_warning": credentials_missing(),
        },
    )


@app.post("/projects/{project_id}/clone")
def clone_repository(db: DbSession, project_id: int) -> RedirectResponse:
    project = _get_project_or_404(db, project_id)
    target = f"/projects/{project.id}"

    result = clone_project(project.github_url, project.owner, project.repo)
    if not isinstance(result, Cloned):
        return _redirect(target, error=f"{result.message} {result.stderr}".strip())

    project.local_path = str(result.path)
    db.commit()
    return _redirect(target, notice=f"Cloned to {result.path}.")


@app.post("/projects/{project_id}/tasks")
def add_task(
    db: DbSession,
    project_id: int,
    title: Annotated[str, Form()],
    body: Annotated[str, Form()] = "",
    # A string, not an int: the "Top level" option submits an empty value, and
    # an int-typed field would reject that as a validation error rather than
    # reading it as "no parent".
    parent_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    project = _get_project_or_404(db, project_id)
    target = f"/projects/{project.id}"

    cleaned = title.strip()
    if not cleaned:
        return _redirect(target, error="Enter a title for the task.")

    parent_key = int(parent_id) if parent_id.strip().isdigit() else None
    if parent_key is not None:
        parent = db.get(Task, parent_key)
        if parent is None or parent.project_id != project.id:
            return _redirect(target, error="That parent task does not belong to this project.")

    # Append within the sibling group rather than at the end of the project, so
    # a new sub-task lands under its siblings instead of jumping the tree.
    siblings = db.scalars(
        select(Task.position).where(Task.project_id == project.id, Task.parent_id == parent_key)
    ).all()
    next_position = (max(siblings) + 1) if siblings else 0

    db.add(
        Task(
            project_id=project.id,
            parent_id=parent_key,
            title=cleaned,
            body=body.strip() or None,
            position=next_position,
        )
    )
    db.commit()
    return _redirect(target)


@app.post("/tasks/{task_id}/status")
def set_task_status(
    db: DbSession, task_id: int, new_status: Annotated[str, Form()]
) -> RedirectResponse:
    task = _get_task_or_404(db, task_id)
    target = f"/projects/{task.project_id}"

    allowed = {"open", "active", "blocked", "done", "cancelled"}
    if new_status not in allowed:
        return _redirect(target, error=f"{new_status!r} is not a task status.")

    task.status = new_status
    db.commit()
    return _redirect(target)


@app.post("/tasks/{task_id}/delete")
def delete_task(db: DbSession, task_id: int) -> RedirectResponse:
    task = _get_task_or_404(db, task_id)
    project = task.project
    target = f"/projects/{project.id}"

    if any(run.status in ("queued", "running") for run in task.runs):
        return _redirect(target, error="This task has a run in progress. Cancel it first.")

    # Remove the worktree before the row, since the path is only recorded here.
    # Children are cascaded by the database but their worktrees are not, so
    # they are collected first.
    for doomed in _task_and_descendants(task):
        if doomed.worktree_path and project.local_path:
            remove_worktree(Path(project.local_path), Path(doomed.worktree_path))

    db.delete(task)
    db.commit()
    return _redirect(target, notice=f"Deleted {task.title!r}.")


def _task_and_descendants(task: Task) -> list[Task]:
    collected = [task]
    for child in task.children:
        collected.extend(_task_and_descendants(child))
    return collected


@app.post("/tasks/{task_id}/runs")
def start_plan_run(db: DbSession, task_id: int) -> RedirectResponse:
    task = _get_task_or_404(db, task_id)
    reap_stale_runs(db)

    result = start_run(db, task, phase="plan")
    if isinstance(result, RunRefused):
        return _redirect(f"/projects/{task.project_id}", error=result.message)
    return _redirect(f"/runs/{result.run.id}")


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def show_run(
    request: Request,
    db: DbSession,
    run_id: int,
    error: str | None = None,
    notice: str | None = None,
) -> HTMLResponse:
    run = _get_run_or_404(db, run_id)
    reap_stale_runs(db)
    db.refresh(run)

    events = db.scalars(
        select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
    ).all()

    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "task": run.task,
            "events": [_render_event(event) for event in events],
            "is_live": run.status not in TERMINAL_RUN_STATUSES and run.status != "awaiting_review",
            "last_seq": events[-1].seq if events else 0,
            "error": error,
            "notice": notice,
        },
    )


def _render_event(event: RunEvent) -> dict:
    """Decode a stored event for display.

    A malformed payload renders as an empty dict rather than breaking the page:
    the run itself is still worth reading.
    """
    try:
        payload = json.loads(event.payload)
    except json.JSONDecodeError:
        payload = {}
    return {"seq": event.seq, "kind": event.kind, "payload": payload}


@app.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: int,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Stream a run's events, replaying anything the client missed.

    The only async handler in the app, and the reason the event log exists. A
    phone that sleeps mid-run reconnects with Last-Event-ID and receives
    everything emitted during the gap before the live tail resumes, so sleeping
    costs nothing rather than silently losing output.
    """
    try:
        start_after = int(last_event_id) if last_event_id else 0
    except ValueError:
        start_after = 0

    return StreamingResponse(
        _event_stream(run_id, start_after),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx and other proxies buffer by default, which holds events
            # until the response ends — exactly wrong for a live stream.
            "X-Accel-Buffering": "no",
        },
    )


#: How often the stream checks for new rows. SQLite has no change
#: notification, so this polls; 0.4s is below the threshold where the page
#: feels laggy and cheap enough for a handful of viewers.
_POLL_SECONDS = 0.4


async def _event_stream(run_id: int, start_after: int) -> AsyncIterator[str]:
    seq = start_after
    while True:
        # The database calls are blocking, so they go to a thread rather than
        # stalling the event loop for every connected client.
        batch, finished = await asyncio.to_thread(_read_events, run_id, seq)

        for event in batch:
            seq = event["seq"]
            yield f"id: {seq}\nevent: {event['kind']}\ndata: {json.dumps(event['payload'])}\n\n"

        if finished:
            yield f"event: done\ndata: {json.dumps({'seq': seq})}\n\n"
            return

        await asyncio.sleep(_POLL_SECONDS)


def _read_events(run_id: int, after_seq: int) -> tuple[list[dict], bool]:
    """One polling step: new events, and whether the run has stopped.

    Reads in that order deliberately — the status is checked after the events
    are fetched, so a run that finishes between the two queries still has its
    final events delivered before the stream closes.
    """
    session = get_session_factory()()
    try:
        events = session.scalars(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
            .order_by(RunEvent.seq)
            .limit(200)
        ).all()
        rendered = [_render_event(event) for event in events]

        run = session.get(Run, run_id)
        if run is None:
            return rendered, True
        session.refresh(run)
        stopped = run.status in TERMINAL_RUN_STATUSES or run.status == "awaiting_review"
        # Only stop once the backlog is drained, or a fast run could close the
        # stream with events still unsent.
        return rendered, stopped and len(rendered) < 200
    finally:
        session.close()


@app.post("/runs/{run_id}/approve")
def approve_plan(db: DbSession, run_id: int) -> RedirectResponse:
    run = _get_run_or_404(db, run_id)

    if run.status != "awaiting_review":
        return _redirect(f"/runs/{run.id}", error=f"This run is {run.status}, not awaiting review.")
    if run.phase != "plan":
        return _redirect(f"/runs/{run.id}", error="Only a planning run can be approved.")

    # Resuming the planning session is what makes the execute phase cheap: the
    # agent still has the codebase context and the plan it just wrote.
    result = start_run(db, run.task, phase="execute", session_id=run.session_id)
    if isinstance(result, RunRefused):
        return _redirect(f"/runs/{run.id}", error=result.message)
    return _redirect(f"/runs/{result.run.id}")


@app.post("/runs/{run_id}/cancel")
def cancel(db: DbSession, run_id: int) -> RedirectResponse:
    run = _get_run_or_404(db, run_id)
    error = cancel_run(db, run)
    return _redirect(f"/runs/{run.id}", error=error)
