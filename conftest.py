"""What every test gets, wherever it lives.

Kept at the repository root because `testpaths` covers both `src/workbench`
and `tests`, and this has to apply to both.
"""

import pytest

from workbench.database.db import reset_engine


@pytest.fixture(autouse=True)
def _close_the_process_wide_engine():
    """Dispose of whatever process-wide engine a test caused to exist.

    Code under test builds one through `get_engine()` whenever it opens a
    session, and nothing else would ever close it. See #83: unclosed
    connections piled up faster than the garbage collector reclaimed them,
    until an agent run's 1,024-file limit ended the suite.
    """
    yield
    reset_engine()
