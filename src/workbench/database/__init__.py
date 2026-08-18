"""The schema and the connection to it.

`models` defines the tables, `db` builds the engine and hands out sessions.
Nothing else in the package should be constructing engines: the pragmas that
make foreign keys and WAL work live in one place on purpose.
"""
