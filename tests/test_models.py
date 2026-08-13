"""Schema behaviour that is easy to get silently wrong.

SQLite ignores foreign keys unless a pragma turns them on, so the cascade in
the model definition is not self-evidently in force. These tests check the
constraints actually bite at runtime rather than just existing in the DDL.
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from workbench.db import make_engine
from workbench.models import Base, Project, User


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _project(user: User, url: str = "https://github.com/idm23/workbench") -> Project:
    return Project(user=user, owner="idm23", repo="workbench", github_url=url)


def test_user_owns_projects(session):
    user = User(name="ian")
    session.add(_project(user))
    session.commit()

    assert session.get(User, user.id).projects[0].repo == "workbench"


def test_created_at_is_populated_automatically(session):
    user = User(name="ian")
    session.add(user)
    session.commit()

    assert user.created_at is not None


def test_same_repo_twice_for_one_user_is_rejected(session):
    user = User(name="ian")
    session.add_all([_project(user), _project(user)])

    with pytest.raises(IntegrityError):
        session.commit()


def test_two_users_may_each_add_the_same_repo(session):
    session.add_all([_project(User(name="ian")), _project(User(name="jake"))])
    session.commit()

    assert session.query(Project).count() == 2


def test_duplicate_user_name_is_rejected(session):
    session.add_all([User(name="ian"), User(name="ian")])

    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_a_user_deletes_their_projects(session):
    user = User(name="ian")
    session.add(_project(user))
    session.commit()

    session.delete(user)
    session.commit()

    assert session.query(Project).count() == 0


def test_foreign_keys_are_enforced(session):
    """Guards the PRAGMA in db.py — without it SQLite accepts this silently."""
    session.add(
        Project(
            user_id=9999,
            owner="idm23",
            repo="workbench",
            github_url="https://github.com/idm23/workbench",
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()
