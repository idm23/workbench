"""The web application: users, their projects, and the forms to add both.

Route handlers are sync `def` rather than `async def` deliberately. FastAPI runs
sync handlers in a threadpool, so the blocking SQLAlchemy and httpx calls here
cannot stall the event loop. When streaming agent output arrives it will be one
`async def` endpoint reading a durable log, not a rewrite of these.

Messages are passed between requests as query parameters rather than flash
state, which keeps the app free of session middleware and a signing secret.
"""

from functools import cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from workbench.api import router as api_router
from workbench.config import instance
from workbench.database.db import get_db
from workbench.database.models import Project, Run, RunPhase, Task, TaskStatus, User
from workbench.git.github import (
    InvalidReference,
    RepoMetadata,
    RepoNotFound,
    fetch_repo_metadata,
    parse_repo_reference,
)
from workbench.git.revision import head_revision
from workbench.git.worktrees import Cloned, clone_project, local_checkout
from workbench.runs.activity import activity_by_task
from workbench.runs.lifecycle import NotCancellable, cancel_run, start_run
from workbench.runs.rate_limits import latest_readings
from workbench.runs.stream import fetch_events, parse_last_event_id, stream
from workbench.tasks import (
    WrongProject,
    build_tree,
    create_task,
    flatten,
    set_status,
)
from workbench.tasks import (
    delete_task as delete_task_and_children,
)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

app = FastAPI(title="Workbench")

# Mounted rather than defined here: the JSON routes are a second face on the
# same operations, not a second implementation of them.
app.include_router(api_router)


#: A run with more events than this is read in pages by the stream rather
#: than rendered in one response. A long agent run is thousands of rows.
MAX_RENDERED_EVENTS = 500


DbSession = Annotated[Session, Depends(get_db)]


def _shared(db: Session, *, limits: bool = True) -> dict:
    """Context every page gets.

    The rate-limit panel is on every page rather than on a run's own because
    the window it describes belongs to the account, not to a run — it is spent
    by whatever else uses the same subscription, and the moment it is worth
    reading is before starting something, which is any page at all.
    """
    if not limits:
        return {"show_limits": False}
    return {"show_limits": True, "rate_limits": latest_readings(db)}


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


@cache
def deployed_revision() -> str:
    """The commit this process is serving.

    Cached, unlike the underlying helper: a running process cannot change
    revision, and a deploy always restarts the service. So this is fixed for
    the life of the process by construction, and shelling out per request would
    be paying for an answer that cannot differ.
    """
    return head_revision()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness, plus what is actually running.

    The revision is here because until now nothing could tell you which commit
    an instance was serving. A deploy moves the checkout and restarts the
    service, and confirming the second half had happened meant reasoning about
    the first — comparing this against `git rev-parse` in the checkout answers
    it directly, and answers it from a phone.

    The instance name is here for the same reason: production and staging
    differ only by port, which is easy to lose track of.
    """
    return {
        "status": "ok",
        "revision": deployed_revision(),
        "instance": instance() or "production",
    }


@app.get("/", response_class=HTMLResponse)
def list_users(request: Request, db: DbSession, error: str | None = None) -> HTMLResponse:
    users = db.scalars(
        # selectinload so rendering each user's project count is one extra
        # query rather than one per user.
        select(User).options(selectinload(User.projects)).order_by(User.name)
    ).all()
    return templates.TemplateResponse(
        request, "users.html", {**_shared(db), "users": users, "error": error}
    )


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
        {**_shared(db), "user": user, "error": error, "notice": notice},
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

    tasks = db.scalars(
        select(Task).where(Task.project_id == project.id).order_by(Task.position, Task.id)
    ).all()

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            **_shared(db),
            "project": project,
            "nodes": flatten(build_tree(list(tasks))),
            # One query for the whole tree. Asking per node is how a page that
            # felt instant stops being one.
            "activity": activity_by_task(db, project.id),
            # Derived, not stored. This database is copied between instances —
            # staging restores production's snapshot on every deploy — so a
            # stored path would arrive pointing at the other machine's disk.
            "checkout": local_checkout(project.owner, project.repo),
            "error": error,
            "notice": notice,
        },
    )


@app.post("/projects/{project_id}/clone")
def clone_repository(db: DbSession, project_id: int) -> RedirectResponse:
    """Clone the project to this machine, so tasks have somewhere to run.

    Nothing needs the clone yet — worktrees arrive with runs. It is here now
    because it is the slow, network-bound half, and finding out that a
    repository is private or misspelled is much better done from a button than
    from the first agent run.
    """
    project = _get_project_or_404(db, project_id)
    target = f"/projects/{project.id}"

    result = clone_project(project.github_url, project.owner, project.repo)
    if not isinstance(result, Cloned):
        return _redirect(target, error=f"{result.message} {result.stderr}".strip())

    # Nothing to write down: the clone's location is derivable, and the next
    # page load asks the filesystem.
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

    if not title.strip():
        return _redirect(target, error="Enter a title for the task.")

    parent_key = int(parent_id) if parent_id.strip().isdigit() else None
    created = create_task(db, project, title=title, body=body, parent_id=parent_key)
    if isinstance(created, WrongProject):
        return _redirect(target, error=created.message)
    return _redirect(target)


@app.post("/tasks/{task_id}/status")
def set_task_status(
    db: DbSession, task_id: int, new_status: Annotated[str, Form()]
) -> RedirectResponse:
    task = _get_task_or_404(db, task_id)
    target = f"/projects/{task.project_id}"

    try:
        status = TaskStatus(new_status)
    except ValueError:
        # The form only ever submits valid values, so this is a hand-crafted
        # request. Say what happened rather than returning a bare 422.
        return _redirect(target, error=f"{new_status!r} is not a task status.")

    set_status(db, task, status)
    return _redirect(target)


@app.post("/tasks/{task_id}/runs")
def start_task_run(
    db: DbSession, task_id: int, phase: Annotated[str, Form()] = "plan"
) -> RedirectResponse:
    """Hand a task to an agent.

    The refusals — the concurrency cap, a task already being worked, an
    executor that would not start — come back as messages rather than errors,
    because every one of them is an ordinary answer to a button press and the
    person reading it is on a phone.
    """
    task = _get_task_or_404(db, task_id)
    target = f"/projects/{task.project_id}"

    try:
        chosen = RunPhase(phase)
    except ValueError:
        return _redirect(target, error=f"{phase!r} is not a run phase.")

    if task.children:
        # A task with children describes work rather than being work, so an
        # agent pointed at one has no single thing to do.
        return _redirect(target, error="Break this into a sub-task and run that instead.")

    result = start_run(db, task, chosen)
    if isinstance(result, Run):
        return _redirect(target, notice=f"Run {result.id} started ({chosen.value}).")
    return _redirect(target, error=result.message)


@app.post("/runs/{run_id}/cancel")
def cancel_task_run(db: DbSession, run_id: int) -> RedirectResponse:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No run with id {run_id}.")

    target = f"/projects/{run.task.project_id}"
    result = cancel_run(db, run)
    if isinstance(result, NotCancellable):
        return _redirect(target, error=result.message)
    return _redirect(target, notice=f"Run {run_id} asked to stop.")


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def show_run(request: Request, db: DbSession, run_id: int) -> HTMLResponse:
    """One run, with everything it has said so far.

    Rendered server-side rather than left to the stream to fill in. A run read
    back a week later has no stream to open, and a page that is blank until
    JavaScript connects is a page that is blank when JavaScript fails.
    """
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No run with id {run_id}.")

    events = fetch_events(db, run_id, after_seq=0, limit=MAX_RENDERED_EVENTS)
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            **_shared(db),
            "run": run,
            "task": run.task,
            "events": events,
            # Where the browser should resume from, so the stream continues the
            # page rather than repeating it.
            "last_seq": events[-1].seq if events else 0,
            "live": not run.status.is_terminal,
        },
    )


@app.get("/runs/{run_id}/events")
async def stream_run_events(request: Request, run_id: int) -> StreamingResponse:
    """The run's event log, as server-sent events.

    The app's one `async def` route, which is why every database read inside it
    goes through `asyncio.to_thread`: the events being streamed are written by
    a different process, so there is nothing to await on but the table.

    Resumable by construction. A browser reconnecting sends `Last-Event-ID`,
    which is the sequence number it last saw, and gets everything after it —
    so a phone that slept through half a run misses nothing.
    """
    resume = request.query_params.get("after") or request.headers.get("last-event-id")

    return StreamingResponse(
        stream(run_id, parse_last_event_id(resume)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tells a buffering reverse proxy not to. Without it the whole
            # point of streaming is lost to a proxy waiting for a full
            # response before sending anything.
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/tasks/{task_id}/delete")
def delete_task(db: DbSession, task_id: int) -> RedirectResponse:
    """Delete a task and everything under it.

    Children, runs, and events go by database cascade. The worktree does not —
    it is a directory on disk that only this row knows the path of, so it has
    to be removed before the row that names it.
    """
    task = _get_task_or_404(db, task_id)
    target = f"/projects/{task.project_id}"

    title = delete_task_and_children(db, task)
    return _redirect(target, notice=f"Deleted {title!r}.")
