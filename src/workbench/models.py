"""Database schema.

Deliberately small: a user owns projects, and a project points at a GitHub repo.
Tasks, runs, and worktrees come later.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[User] = relationship(back_populates="projects")

    def __repr__(self) -> str:
        return f"<Project id={self.id} {self.owner}/{self.repo}>"
