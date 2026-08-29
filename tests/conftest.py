"""Guards that apply to every test, whichever module it lives in.

Both of these exist because a test reached out and touched the machine it was
running on. Module-scoped fixtures could not have stopped it: the call was in
`test_units.py`, and the fixture that would have caught it was in
`test_deploy.py`.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point `data/` at a temporary directory for the duration of a test.

    Anything derived from `database_path()` follows — the database, the clones,
    the worktrees, and the deployer's marker files. Without this a test that
    records a revision writes into the developer's own checkout, and the next
    run reads it back: the deployer then believes the running service is stale
    and tries to restart it, in a unit test, on whatever machine is running the
    suite.

    That is not hypothetical. It is how this file came to exist.

    Tests that want a specific database still set `WORKBENCH_DB` themselves;
    their `monkeypatch.setenv` runs after this and wins.
    """
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "workbench.db"))


@pytest.fixture(autouse=True)
def no_service_restarts(monkeypatch):
    """Never let a test restart a systemd service.

    Belt to the braces above: with `data/` isolated there is no marker to make
    the service look stale, so this should never fire. It is here because the
    consequence if it does is a test suite restarting a real service on a real
    machine, and that is worth two lines to make impossible rather than
    unlikely.

    Tests that mean to exercise the restart patch `deploy._run` instead, which
    is below this.
    """
    monkeypatch.setattr(
        "workbench.deploy.restart_service",
        lambda: pytest.fail("a test tried to restart a real service"),
    )
