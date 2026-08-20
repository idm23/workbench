"""The web application: users, their projects, and the forms to add both.

Route handlers are sync `def` rather than `async def` deliberately. FastAPI runs
sync handlers in a threadpool, so the blocking SQLAlchemy and httpx calls here
cannot stall the event loop. When streaming agent output arrives it will be one
`async def` endpoint reading a durable log, not a rewrite of these.

Messages are passed between requests as query parameters rather than flash
state, which keeps the app free of session middleware and a signing secret.
"""

from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from workbench.config import instance
from workbench.database.db import get_session_factory
from workbench.database.models import Project, Task, TaskStatus, User
from workbench.git.github import (
    InvalidReference,
    RepoMetadata,
    RepoNotFound,
    fetch_repo_metadata,
    parse_repo_reference,
)
from workbench.git.revision import head_revision
from workbench.git.worktrees import Cloned, clone_project, remove_worktree
from workbench.tasks import build_tree, flatten

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

    tasks = db.scalars(
        select(Task).where(Task.project_id == project.id).order_by(Task.position, Task.id)
    ).all()

    return templates.TemplateResponse(
        request,
        "project_detail.html",
        {
            "project": project,
            "nodes": flatten(build_tree(list(tasks))),
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

    try:
        task.status = TaskStatus(new_status)
    except ValueError:
        # The form only ever submits valid values, so this is a hand-crafted
        # request. Say what happened rather than returning a bare 422.
        return _redirect(target, error=f"{new_status!r} is not a task status.")

    db.commit()
    return _redirect(target)


@app.post("/tasks/{task_id}/delete")
def delete_task(db: DbSession, task_id: int) -> RedirectResponse:
    """Delete a task and everything under it.

    Children, runs, and events go by database cascade. The worktree does not —
    it is a directory on disk that only this row knows the path of, so it has
    to be removed before the row that names it.
    """
    task = _get_task_or_404(db, task_id)
    project = task.project
    target = f"/projects/{project.id}"
    title = task.title

    for doomed in _task_and_descendants(task):
        if doomed.worktree_path and project.local_path:
            remove_worktree(Path(project.local_path), Path(doomed.worktree_path))

    db.delete(task)
    db.commit()
    return _redirect(target, notice=f"Deleted {title!r}.")


def _task_and_descendants(task: Task) -> list[Task]:
    collected = [task]
    for child in task.children:
        collected.extend(_task_and_descendants(child))
    return collected
