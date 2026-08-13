"""The web application: users, their projects, and the forms to add both.

Route handlers are sync `def` rather than `async def` deliberately. FastAPI runs
sync handlers in a threadpool, so the blocking SQLAlchemy and httpx calls here
cannot stall the event loop. When streaming agent output arrives it will be one
`async def` endpoint reading a durable log, not a rewrite of these.

Messages are passed between requests as query parameters rather than flash
state, which keeps the app free of session middleware and a signing secret.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from workbench.db import get_session_factory
from workbench.github import (
    InvalidReference,
    RepoMetadata,
    RepoNotFound,
    fetch_repo_metadata,
    parse_repo_reference,
)
from workbench.models import Project, User

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
