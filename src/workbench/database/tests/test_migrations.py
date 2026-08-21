"""Migrations run against a database that already has rows in it.

Every other migration check in this repository runs against an empty database,
where a migration cannot fail in the ways that matter. A `NOT NULL` column with
no server default, a unique constraint that real data violates, a type change
SQLite's batch rewrite cannot perform on populated tables — all of them pass
cleanly on empty tables and fail on the server.

The automatic deployer applies migrations without anyone watching, and staging
migrates a copy of production for exactly this reason. These tests are the same
idea, cheap enough to run on every commit.
"""

import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.orm import Session

from workbench.config import repo_root
from workbench.database.db import make_engine
from workbench.database.models import Project, Run, RunEvent, RunPhase, Task, User


@pytest.fixture
def alembic_config(tmp_path, monkeypatch) -> Config:
    """Alembic pointed at a scratch database rather than the developer's."""
    database = tmp_path / "migrate.db"
    monkeypatch.setenv("WORKBENCH_DB", str(database))

    config = Config(str(repo_root() / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root() / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database}")
    config.attributes["configure_logger"] = False
    config.attributes["database_path"] = database
    return config


def revisions(config: Config) -> list[str]:
    """Every revision, oldest first."""
    script = ScriptDirectory.from_config(config)
    return [revision.revision for revision in reversed(list(script.walk_revisions()))]


def database_of(config: Config):
    return config.attributes["database_path"]


def seed_previous_revision(database) -> None:
    """Populate the tables as they exist one revision before head.

    Written as raw SQL on purpose. Using the ORM would insert against *today's*
    models, which is precisely the shape the migration is supposed to produce —
    the rows have to predate it to be a real test.
    """
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO users (id, name, created_at) VALUES (1, 'ian', '2026-01-01 00:00:00')"
        )
        connection.executemany(
            "INSERT INTO projects (id, user_id, owner, repo, github_url, created_at)"
            " VALUES (?, 1, ?, ?, ?, '2026-01-01 00:00:00')",
            [
                (1, "idm23", "workbench", "https://github.com/idm23/workbench"),
                (2, "idm23", "other", "https://github.com/idm23/other"),
            ],
        )


def test_every_revision_applies_in_order(alembic_config):
    """One at a time, not straight to head — a broken middle step is invisible
    when the whole chain is applied in one call."""
    for revision in revisions(alembic_config):
        command.upgrade(alembic_config, revision)

    with sqlite3.connect(database_of(alembic_config)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert {"users", "projects", "tasks", "runs", "run_events"} <= tables


def test_upgrading_over_existing_rows_preserves_them(alembic_config):
    """The case an empty-database migration test cannot fail on."""
    all_revisions = revisions(alembic_config)
    command.upgrade(alembic_config, all_revisions[-2])
    seed_previous_revision(database_of(alembic_config))

    command.upgrade(alembic_config, "head")

    with sqlite3.connect(database_of(alembic_config)) as connection:
        users = connection.execute("SELECT name FROM users").fetchall()
        projects = connection.execute("SELECT owner, repo FROM projects ORDER BY id").fetchall()

    assert users == [("ian",)]
    assert projects == [("idm23", "workbench"), ("idm23", "other")]


def test_columns_added_to_populated_tables_are_nullable(alembic_config):
    """A NOT NULL column with no default cannot be added to existing rows.

    It passes on an empty table and fails on the server, which is the exact
    trap this file exists for.
    """
    all_revisions = revisions(alembic_config)
    command.upgrade(alembic_config, all_revisions[-2])
    seed_previous_revision(database_of(alembic_config))

    command.upgrade(alembic_config, "head")

    with sqlite3.connect(database_of(alembic_config)) as connection:
        rows = connection.execute("SELECT setup_command, agent_backend FROM projects").fetchall()

    assert rows == [(None, None), (None, None)]


def test_dropping_a_column_keeps_the_rest_of_the_row(alembic_config):
    """SQLite cannot DROP COLUMN in place, so Alembic rebuilds the table.

    A batch rewrite that gets the column list wrong silently loses data in the
    columns it did not mean to touch, which no schema comparison would notice.
    """
    all_revisions = revisions(alembic_config)
    command.upgrade(alembic_config, all_revisions[-2])
    seed_previous_revision(database_of(alembic_config))

    command.upgrade(alembic_config, "head")

    with sqlite3.connect(database_of(alembic_config)) as connection:
        projects = connection.execute(
            "SELECT owner, repo, github_url FROM projects ORDER BY id"
        ).fetchall()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}

    assert projects == [
        ("idm23", "workbench", "https://github.com/idm23/workbench"),
        ("idm23", "other", "https://github.com/idm23/other"),
    ]
    assert "local_path" not in columns
    assert {"owner", "repo", "github_url", "setup_command", "agent_backend"} <= columns


def test_downgrade_and_upgrade_again_with_data_present(alembic_config):
    """Reversibility is what makes a bad deploy recoverable by hand."""
    all_revisions = revisions(alembic_config)
    command.upgrade(alembic_config, all_revisions[-2])
    seed_previous_revision(database_of(alembic_config))
    command.upgrade(alembic_config, "head")

    command.downgrade(alembic_config, "-1")
    command.upgrade(alembic_config, "head")

    with sqlite3.connect(database_of(alembic_config)) as connection:
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_the_migrated_schema_accepts_todays_models(alembic_config):
    """The schema Alembic builds and the schema the app expects are the same.

    `alembic check` compares metadata; this writes a row through every relation
    the app actually uses, which is the part that fails when a column exists
    with the wrong type or nullability.
    """
    command.upgrade(alembic_config, "head")

    engine = make_engine(f"sqlite+pysqlite:///{database_of(alembic_config)}")
    with Session(engine) as session:
        user = User(name="ian")
        project = Project(
            user=user,
            owner="idm23",
            repo="workbench",
            github_url="https://github.com/idm23/workbench",
        )
        parent = Task(project=project, title="parent")
        session.add(parent)
        session.commit()

        child = Task(project=project, parent_id=parent.id, title="child")
        session.add(child)
        session.commit()

        run = Run(task_id=child.id, phase=RunPhase.PLAN, backend="claude", model="a-model")
        session.add(run)
        session.commit()

        session.add(RunEvent(run_id=run.id, seq=1, kind="text", payload={"text": "hello"}))
        session.commit()

        assert session.query(RunEvent).one().payload == {"text": "hello"}


def test_foreign_keys_survive_the_batch_rewrites(alembic_config):
    """SQLite cannot ALTER most constraints, so Alembic rebuilds whole tables.

    A batch rewrite that forgets a foreign key leaves a schema that looks right
    and silently stops cascading, which would orphan runs behind deleted tasks.
    """
    command.upgrade(alembic_config, "head")

    with sqlite3.connect(database_of(alembic_config)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        keys = {
            table: [row[2] for row in connection.execute(f"PRAGMA foreign_key_list({table})")]
            for table in ("projects", "tasks", "runs", "run_events")
        }

    assert keys["projects"] == ["users"]
    assert sorted(keys["tasks"]) == ["projects", "tasks"]
    assert keys["runs"] == ["tasks"]
    assert keys["run_events"] == ["runs"]


def test_cascade_still_bites_after_migrating(alembic_config):
    """The end-to-end version of the check above, through real deletes."""
    command.upgrade(alembic_config, "head")

    engine = make_engine(f"sqlite+pysqlite:///{database_of(alembic_config)}")
    with Session(engine) as session:
        user = User(name="ian")
        project = Project(
            user=user,
            owner="idm23",
            repo="workbench",
            github_url="https://github.com/idm23/workbench",
        )
        task = Task(project=project, title="a task")
        session.add(task)
        session.commit()
        run = Run(task_id=task.id, phase=RunPhase.PLAN)
        session.add(run)
        session.commit()
        session.add(RunEvent(run_id=run.id, seq=1, kind="text", payload={}))
        session.commit()

        session.delete(user)
        session.commit()

        for table in ("projects", "tasks", "runs", "run_events"):
            remaining = session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            assert remaining == 0, table
