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
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from workbench.api import router as api_router
from workbench.config import instance, systemd_available
from workbench.database.db import get_db
from workbench.database.models import (
    Project,
    Run,
    RunEventKind,
    RunPhase,
    RunStatus,
    Task,
    TaskStatus,
    User,
)
from workbench.doctor import page_warnings
from workbench.git.github import (
    InvalidReference,
    RepoMetadata,
    RepoNotFound,
    fetch_repo_metadata,
    parse_repo_reference,
)
from workbench.git.revision import head_revision
from workbench.git.worktrees import (
    Cloned,
    Synced,
    SyncRefused,
    clone_project,
    local_checkout,
    sync_worktree,
)
from workbench.rendering import render_markdown
from workbench.runs.activity import activity_by_task, pr_url_by_task, project_activity_fingerprint
from workbench.runs.lifecycle import (
    NotCancellable,
    active_run_for_project,
    active_run_for_task,
    cancel_run,
    continue_run,
    start_conversation,
    start_run,
)
from workbench.runs.rate_limits import latest_readings
from workbench.runs.store import append_event, append_input
from workbench.runs.stream import fetch_events, parse_last_event_id, stream
from workbench.services import active_shells, running_services
from workbench.tasks import (
    WrongProject,
    archive_task,
    build_tree,
    create_subtask,
    create_task,
    flatten,
    ready_for_review,
    ready_to_execute,
    set_status,
    unarchive_task,
)
from workbench.tasks import (
    delete_task as delete_task_and_children,
)
from workbench.tasks.origin import DEFAULT as DEFAULT_ORIGIN
from workbench.tasks.origin import InvalidOrigin, origin_branch_for, origin_choices, resolve_origin

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# A plan, a summary, and a run's own `text`/`thinking` events are Markdown —
# see rendering.py for why — so templates reach for `| markdown` wherever
# they show one of those *in full*. Never applied to a character-truncated
# preview; see that module's docstring for why not.
templates.env.filters["markdown"] = render_markdown

app = FastAPI(title="Workbench")

# Mounted rather than defined here: the JSON routes are a second face on the
# same operations, not a second implementation of them.
app.include_router(api_router)

# The site icon (favicons, apple-touch-icon, manifest icons) and its
# manifest — everything else is inline in base.html, so this is the app's
# only static asset directory. Read straight from the checkout via
# `__file__`, same as `templates` above, so it works under the editable
# install `uv sync` produces without any packaging step.
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


#: A run with more events than this is read in pages by the stream rather
#: than rendered in one response. A long agent run is thousands of rows.
MAX_RENDERED_EVENTS = 500


DbSession = Annotated[Session, Depends(get_db)]


def _shared(db: Session) -> dict:
    """Context every page gets.

    The rate-limit panel is on every page rather than on a run's own because
    the window it describes belongs to the account, not to a run — it is spent
    by whatever else uses the same subscription, and the moment it is worth
    reading is before starting something, which is any page at all.

    Setup warnings are here for a stronger version of the same reason. The
    install says them once, into a terminal nobody re-reads, and the state they
    describe changes long afterwards. A tree offering to start an agent that
    cannot authenticate is misleading on every page it appears on.

    There is deliberately no way for a page to switch either of them off. A
    warning some page could suppress is one you cannot trust the absence of.
    """
    return {"rate_limits": latest_readings(db), "warnings": page_warnings()}


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


@app.get("/services", response_class=HTMLResponse)
def show_services(request: Request, db: DbSession) -> HTMLResponse:
    """What this instance of Workbench is actually running right now.

    Scoped to this instance on purpose — production and staging share a
    machine, and `running_services` reads through the same instance-scoped
    unit names the installer itself writes, so a page opened on one never
    lists the other's units.
    """
    return templates.TemplateResponse(
        request,
        "services.html",
        {
            **_shared(db),
            "services": running_services(db),
            "shells": active_shells(db),
            "systemd_available": systemd_available(),
        },
    )


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
        select(Task)
        .where(Task.project_id == project.id, Task.archived_at.is_(None))
        .order_by(Task.position, Task.id)
    ).all()
    archived_count = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.project_id == project.id, Task.archived_at.is_not(None))
    )

    nodes = flatten(build_tree(list(tasks)))
    # One query for the whole tree. Asking per node is how a page that felt
    # instant stops being one.
    activity = activity_by_task(db, project.id)
    # A finished task's pull request, if one was opened — surfaced directly on
    # the tree rather than only on the run that opened it.
    pr_urls = pr_url_by_task(db, project.id)
    # What the page started with, so the poll script below can tell "nothing
    # has changed" from "something has" without re-rendering anything itself.
    activity_version = project_activity_fingerprint(db, project.id)
    # Derived, not stored. This database is copied between instances — staging
    # restores production's snapshot on every deploy — so a stored path would
    # arrive pointing at the other machine's disk.
    checkout = local_checkout(project.owner, project.repo)

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            **_shared(db),
            "project": project,
            "nodes": nodes,
            "archived_count": archived_count,
            "activity": activity,
            "pr_urls": pr_urls,
            "activity_version": activity_version,
            # Tasks one click away from starting or continuing execution —
            # promoted above the tree so the thing most worth doing on the
            # page is the first thing it offers, not something to scroll for.
            "ready": ready_to_execute(nodes, activity, pr_urls, checkout=bool(checkout)),
            # Tasks whose most recent run opened a pull request that's still
            # open — the complement of `ready`'s own exclusion of tasks with a
            # `pr_url`, promoted the same way for the same reason: the PR link
            # and the buttons to close the loop shouldn't be something to
            # scroll for either.
            "for_review": ready_for_review(nodes, activity, pr_urls),
            # The project's own standing conversation, if one is in flight —
            # what lets the page offer "Continue" instead of "Talk to this
            # project" without a second click to find out.
            "conversation": active_run_for_project(db, project.id),
            "checkout": checkout,
            # Only worth computing for a task that would actually show the
            # picker: one with no worktree yet has nothing to choose between.
            # The option the picker preselects for a task that has never been
            # started. Passed in rather than spelled in the template, so the
            # reasoning lives beside the constant.
            "default_origin": DEFAULT_ORIGIN,
            "origin_choices": {
                task.id: origin_choices(task)
                for task in tasks
                if not task.children and task.worktree_path is None
            },
            "error": error,
            "notice": notice,
        },
    )


@app.get("/projects/{project_id}/activity-version")
def project_activity_version(db: DbSession, project_id: int) -> dict[str, str]:
    """What `project_detail.html` polls to notice the tree has gone stale.

    Cheap on purpose: two aggregate queries, no template render, no join to
    anything the page itself needs. The client only ever compares this
    against the value it started with — see `project_activity_fingerprint`
    for what actually goes into it.
    """
    project = _get_project_or_404(db, project_id)
    return {"version": project_activity_fingerprint(db, project.id)}


@app.post("/projects/{project_id}/conversation")
def start_project_conversation(db: DbSession, project_id: int) -> RedirectResponse:
    """Talk directly to a project, not any one task within it.

    Redirects into an already-running conversation rather than starting a
    second one — this is what makes clicking the project resume the standing
    conversation instead of duplicating it.
    """
    project = _get_project_or_404(db, project_id)

    existing = active_run_for_project(db, project.id)
    if existing is not None:
        return _redirect(f"/runs/{existing.id}")

    result = start_conversation(db, project)
    if isinstance(result, Run):
        return _redirect(f"/runs/{result.id}")
    return _redirect(f"/projects/{project.id}", error=result.message)


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
    db: DbSession,
    task_id: int,
    phase: Annotated[str, Form()] = "plan",
    origin: Annotated[str, Form()] = "",
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

    if task.worktree_path is None:
        # Nothing to choose once a worktree already exists — its branch is
        # fixed, and every run after the first only ever resumes it.
        resolved = resolve_origin(task, origin or None)
        if isinstance(resolved, InvalidOrigin):
            return _redirect(target, error=resolved.message)
        task.origin_ref = origin or None
        db.commit()

    result = start_run(db, task, chosen)
    if isinstance(result, Run):
        return _redirect(target, notice=f"Run {result.id} started ({chosen.value}).")
    return _redirect(target, error=result.message)


@app.post("/runs/{run_id}/approve")
def approve_plan(db: DbSession, run_id: int) -> RedirectResponse:
    """Act on a reviewed plan: create what it proposed, or carry it out.

    The one button a plan run's `awaiting_review` state has been missing —
    without it, approving a plan and starting the execute phase was never
    actually reachable from the page. What it does depends on what the plan
    itself decided: a decomposition creates the subtasks it proposed, and
    nothing else — either happens once you have a chance to see it, the
    same "look before it runs" a plan run's whole existence is for.
    """
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No run with id {run_id}.")

    task = run.task
    if task is None:
        # A plan run always belongs to a task — only a conversation does
        # not, and a conversation is never phase PLAN — but this is the
        # data this route needs, so ask for it explicitly rather than
        # crash on the assumption.
        target = f"/projects/{run.project_id}" if run.project_id else "/"
        return _redirect(target, error=f"Run {run_id} has no plan awaiting approval.")
    target = f"/projects/{task.project_id}"

    if run.phase is not RunPhase.PLAN or run.status is not RunStatus.AWAITING_REVIEW:
        return _redirect(target, error=f"Run {run_id} has no plan awaiting approval.")

    proposed = (run.proposed_subtasks or {}).get("subtasks", [])
    if proposed:
        for subtask in proposed:
            create_subtask(
                db,
                task,
                title=subtask["title"],
                body=subtask.get("body"),
                ready_to_execute=bool(subtask.get("ready_to_execute")),
            )
        return _redirect(target, notice=f"Created {len(proposed)} subtask(s) from the plan.")

    result = start_run(db, task, RunPhase.EXECUTE)
    if isinstance(result, Run):
        return _redirect(target, notice=f"Run {result.id} started (execute).")
    return _redirect(target, error=result.message)


@app.post("/tasks/{task_id}/sync")
def sync_task(db: DbSession, task_id: int) -> RedirectResponse:
    """Fast-forward a task's branch onto its origin, from a phone.

    A task's branch is set once, when its worktree is first created, and
    nothing brings it forward after that — so a task left sitting between
    planning and approval quietly falls behind whatever its origin has since
    gained. This is the fix that does not need a terminal: refuses rather
    than reconciling, exactly like the checks a deploy already makes.
    """
    task = _get_task_or_404(db, task_id)
    target = f"/projects/{task.project_id}"

    if task.worktree_path is None:
        return _redirect(target, error="This task has no worktree yet.")

    if active_run_for_task(db, task.id) is not None:
        return _redirect(target, error="Wait for the run in progress to finish first.")

    checkout = local_checkout(task.project.owner, task.project.repo)
    if checkout is None:
        return _redirect(
            target, error=f"{task.project.owner}/{task.project.repo} is not cloned here."
        )

    base_branch = origin_branch_for(task)
    result = sync_worktree(checkout, Path(task.worktree_path), base_branch)
    if isinstance(result, Synced):
        return _redirect(target, notice=f"Synced with {base_branch}.")
    if isinstance(result, SyncRefused):
        return _redirect(target, error=result.message)
    return _redirect(target, error=f"{result.message} {result.stderr}".strip())


@app.post("/runs/{run_id}/continue")
def continue_finished_run(db: DbSession, run_id: int) -> RedirectResponse:
    """Reopen a finished run as a conversation.

    A plan or execute run ends the moment the agent is done rather than
    waiting five minutes on the chance somebody types — so this is how a
    dialog gets started, deliberately, when one is actually wanted.

    Redirects to the *new* run, because that is where the conversation is.
    The old one keeps its own page and its own record.
    """
    source = db.get(Run, run_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No run with id {run_id}.")

    target = f"/runs/{run_id}"
    result = continue_run(db, source)
    if isinstance(result, Run):
        return _redirect(f"/runs/{result.id}")
    # Every refusal — the cap, a run already in flight on this task, a run
    # that left no session — is an ordinary answer to a button press, and
    # says so where the button was.
    return _redirect(target, error=result.message)


@app.post("/runs/{run_id}/cancel")
def cancel_task_run(db: DbSession, run_id: int) -> RedirectResponse:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No run with id {run_id}.")

    target = f"/projects/{run.task.project_id if run.task else run.project_id}"
    result = cancel_run(db, run)
    if isinstance(result, NotCancellable):
        return _redirect(target, error=result.message)
    return _redirect(target, notice=f"Run {run_id} asked to stop.")


@app.post("/runs/{run_id}/message")
def send_run_message(db: DbSession, run_id: int, body: Annotated[str, Form()]) -> RedirectResponse:
    """Type something into a run that is still going.

    Written to two places: `run_inputs`, which is all the runner itself ever
    reads (polled the same way `stream.py` already polls `run_events`), and
    immediately as a `run_events` row too, so it shows up on this page and in
    the live stream the instant it's sent rather than only once the runner
    notices it.
    """
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No run with id {run_id}.")

    target = f"/runs/{run_id}"
    cleaned = body.strip()
    if not cleaned:
        return _redirect(target, error="Type something first.")
    if run.status is not RunStatus.RUNNING:
        return _redirect(target, error="This run is not active.")

    append_input(db, run.id, cleaned)
    append_event(db, run.id, RunEventKind.INPUT, {"text": cleaned})
    return _redirect(target)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def show_run(
    request: Request,
    db: DbSession,
    run_id: int,
    error: str | None = None,
    notice: str | None = None,
) -> HTMLResponse:
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
            "project": run.task.project if run.task else run.project,
            "events": events,
            # Where the browser should resume from, so the stream continues the
            # page rather than repeating it.
            "last_seq": events[-1].seq if events else 0,
            "live": not run.status.is_terminal,
            "error": error,
            "notice": notice,
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


@app.post("/tasks/{task_id}/archive")
def archive_task_route(db: DbSession, task_id: int) -> RedirectResponse:
    """Take a finished task off the tree without deleting it.

    Nothing here checks that the task is actually finished — the button
    offering this only renders for one that is, and a task archived by
    mistake is one `Unarchive` away from back exactly where it was.
    """
    task = _get_task_or_404(db, task_id)
    target = f"/projects/{task.project_id}"

    title = archive_task(db, task)
    return _redirect(target, notice=f"Archived {title!r}.")


@app.post("/tasks/{task_id}/unarchive")
def unarchive_task_route(db: DbSession, task_id: int) -> RedirectResponse:
    task = _get_task_or_404(db, task_id)
    target = f"/projects/{task.project_id}/archive"

    title = unarchive_task(db, task)
    return _redirect(target, notice=f"Restored {title!r}.")


@app.get("/projects/{project_id}/archive", response_class=HTMLResponse)
def show_archive(request: Request, db: DbSession, project_id: int) -> HTMLResponse:
    """Everything archived out of this project's tree, so putting a task away
    is never the same thing as losing track of it."""
    project = _get_project_or_404(db, project_id)

    tasks = db.scalars(
        select(Task)
        .where(Task.project_id == project.id, Task.archived_at.is_not(None))
        .order_by(Task.position, Task.id)
    ).all()

    return templates.TemplateResponse(
        request,
        "project_archive.html",
        {
            **_shared(db),
            "project": project,
            "nodes": flatten(build_tree(list(tasks))),
            "pr_urls": pr_url_by_task(db, project.id),
        },
    )
