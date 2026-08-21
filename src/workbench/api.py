"""A JSON API over projects and tasks.

Exists so tasks can be created by something other than a person with a phone —
an agent, a script, or a terminal. It performs exactly the same writes as the
HTML forms, through `workbench.tasks.store`, so the two cannot drift.

**There is no authentication, and that is not an oversight here.** These routes
are exactly as exposed as the forms they mirror: both are reachable by anything
that can reach the app, which is a two-device personal tailnet. Adding a token
to the JSON half while leaving the HTML half open would be theatre. If the app
ever gains auth it belongs in front of both.

FastAPI publishes an OpenAPI schema for these at `/docs`, which is the fastest
way to see what the shapes actually are.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from workbench.database.db import get_db
from workbench.database.models import Project, Task, TaskStatus
from workbench.git.worktrees import local_checkout
from workbench.tasks import TaskNode, WrongProject, build_tree, create_task, delete_task, set_status

router = APIRouter(prefix="/api", tags=["api"])


class ProjectSummary(BaseModel):
    id: int
    owner: str
    repo: str
    description: str | None
    default_branch: str | None
    #: Derived from the filesystem, never stored — see git.worktrees.
    cloned: bool
    open_tasks: int


class TaskOut(BaseModel):
    id: int
    title: str
    body: str | None
    status: TaskStatus
    parent_id: int | None
    #: Nested rather than flat: it mirrors what the page renders, and reading a
    #: tree back as a tree is the point of asking for one.
    children: list[TaskOut]


class TaskIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str | None = None
    parent_id: int | None = None


class TaskPatch(BaseModel):
    """Every field optional — a patch says what changes, not what the task is."""

    title: str | None = Field(default=None, min_length=1, max_length=300)
    body: str | None = None
    status: TaskStatus | None = None


DbSession = Annotated[Session, Depends(get_db)]


def _as_out(node: TaskNode) -> TaskOut:
    return TaskOut(
        id=node.task.id,
        title=node.task.title,
        body=node.task.body,
        status=node.task.status,
        parent_id=node.task.parent_id,
        children=[_as_out(child) for child in node.children],
    )


def _project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No project with id {project_id}.")
    return project


def _task_or_404(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No task with id {task_id}.")
    return task


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects(db: DbSession) -> list[ProjectSummary]:
    """Every project, with enough to pick one and see if it needs cloning."""
    projects = db.scalars(select(Project).order_by(Project.id)).all()
    return [
        ProjectSummary(
            id=project.id,
            owner=project.owner,
            repo=project.repo,
            description=project.description,
            default_branch=project.default_branch,
            cloned=local_checkout(project.owner, project.repo) is not None,
            open_tasks=sum(1 for task in project.tasks if task.status is not TaskStatus.DONE),
        )
        for project in projects
    ]


@router.get("/projects/{project_id}/tasks", response_model=list[TaskOut])
def list_tasks(db: DbSession, project_id: int) -> list[TaskOut]:
    """The project's tasks, as a tree of roots."""
    project = _project_or_404(db, project_id)
    tasks = db.scalars(
        select(Task).where(Task.project_id == project.id).order_by(Task.position, Task.id)
    ).all()
    return [_as_out(node) for node in build_tree(list(tasks))]


@router.post(
    "/projects/{project_id}/tasks",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
)
def add_task(db: DbSession, project_id: int, incoming: TaskIn) -> TaskOut:
    project = _project_or_404(db, project_id)
    created = create_task(
        db,
        project,
        title=incoming.title,
        body=incoming.body,
        parent_id=incoming.parent_id,
    )
    if isinstance(created, WrongProject):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, created.message)

    # Freshly created, so it has no children yet — build_tree would be a query
    # for an answer already known.
    return TaskOut(
        id=created.id,
        title=created.title,
        body=created.body,
        status=created.status,
        parent_id=created.parent_id,
        children=[],
    )


@router.patch("/tasks/{task_id}", response_model=TaskOut)
def update_task(db: DbSession, task_id: int, patch: TaskPatch) -> TaskOut:
    task = _task_or_404(db, task_id)

    if patch.title is not None:
        task.title = patch.title.strip()
    if patch.body is not None:
        task.body = patch.body.strip() or None
    if patch.status is not None:
        set_status(db, task, patch.status)
    db.commit()

    return _as_out(build_tree([task])[0])


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_task(db: DbSession, task_id: int) -> None:
    """Delete a task and everything under it, worktrees included."""
    delete_task(db, _task_or_404(db, task_id))
