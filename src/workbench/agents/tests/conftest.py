"""Guards for the backend tests, in the spirit of the root `tests/conftest.py`.

That file exists because a test reached out and touched the machine running
it. This one exists because a test could now read the *developer's* Claude
credential: `credential_status()` reads a file under `$HOME` to find out how
long the login has left, so without this the suite's verdict would depend on
when whoever ran it last signed in — passing on a laptop, failing on the
server, and vice versa a fortnight later.

Isolating it here rather than in the two tests that need it today is
deliberate: the root conftest's own note is that the fixture which would have
caught the last one lived in the wrong file.
"""

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point `$HOME` at an empty directory, which reads as "no opinion"."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point `data/` at a temporary directory, as the root conftest does.

    That one lives in `tests/` and so does not reach the suites that sit
    beside their code. It matters here because the local backend keeps its
    transcripts under `data/sessions/`: without this, running the tests would
    write conversations into the developer's own checkout and, worse, resume
    from them.
    """
    monkeypatch.setenv("WORKBENCH_DB", str(tmp_path / "data" / "workbench.db"))
