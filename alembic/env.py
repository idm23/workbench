"""Alembic environment.

The database URL comes from workbench.config rather than alembic.ini so there is
exactly one source of truth, and so `alembic upgrade head` inside install.sh
targets the same file the application will open.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from workbench.config import database_url, ensure_data_dir
from workbench.database.models import Base

config = context.config

# Skipped when Alembic is driven programmatically (workbench.install): the
# caller has already configured logging, and fileConfig would disable those
# handlers. This attribute is Alembic's documented hook for that case.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# On a fresh clone data/ does not exist yet, and SQLite will not create a
# database in a missing directory.
ensure_data_dir()
config.set_main_option("sqlalchemy.url", database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rebuilds the
            # table instead. Harmless on other backends, essential on this one.
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
