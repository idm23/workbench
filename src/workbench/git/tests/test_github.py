"""Reference parsing, and opening a pull request.

The parser is the fiddly part and runs on every add, so it is worth pinning
down. The repository *lookup* is still not mocked: it is a read, on a page
someone is watching, and at this size maintaining a fake costs more than it
catches.

Opening a pull request is mocked, and the difference is worth stating. It is a
write, it happens at the very end of a run in a detached process nobody is
watching, and its interesting cases — a duplicate, a bad base branch, a token
that lost its scope — are all ones where getting the answer wrong means a run
that claims a pull request nobody can find, or a traceback where a notice
should have been.

Telling a duplicate apart from a bad base branch — both arrive as a bare 422 —
is the one piece with real logic in this module, and it is worth pinning
precisely: a run once reported "duplicate pull request" for a base branch
that had simply never been pushed to GitHub, because every 422 was assumed to
mean the same thing.
"""

import pytest

from workbench.git import github
from workbench.git.github import (
    InvalidReference,
    PullRequestFailed,
    PullRequestOpened,
    RepoRef,
    open_pull_request,
    parse_repo_reference,
)


@pytest.mark.parametrize(
    "raw",
    [
        "https://github.com/idm23/workbench",
        "https://github.com/idm23/workbench/",
        "https://github.com/idm23/workbench.git",
        "https://github.com/idm23/workbench.git/",
        "http://github.com/idm23/workbench",
        "https://www.github.com/idm23/workbench",
        "git@github.com:idm23/workbench.git",
        "git@github.com:idm23/workbench",
        "idm23/workbench",
        "  idm23/workbench  ",
    ],
)
def test_accepted_forms_all_resolve_to_the_same_repo(raw):
    assert parse_repo_reference(raw) == RepoRef(owner="idm23", repo="workbench")


def test_canonical_url_is_rebuilt_rather_than_echoed():
    """Storing the canonical form keeps duplicate detection working."""
    ref = parse_repo_reference("git@github.com:idm23/workbench.git")

    assert isinstance(ref, RepoRef)
    assert ref.url == "https://github.com/idm23/workbench"
    assert ref.slug == "idm23/workbench"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "workbench",
        "https://gitlab.com/idm23/workbench",
        "https://github.com/idm23",
        "https://github.com/idm23/workbench/tree/main/src",
        "not a url at all",
        "https://example.com/github.com/idm23/workbench",
    ],
)
def test_rejected_forms(raw):
    assert isinstance(parse_repo_reference(raw), InvalidReference)


def test_rejection_explains_what_was_expected():
    result = parse_repo_reference("nonsense")

    assert isinstance(result, InvalidReference)
    assert "owner/repo" in result.message


def test_empty_input_gets_its_own_message():
    """A blank field is a different mistake from a malformed one."""
    result = parse_repo_reference("   ")

    assert isinstance(result, InvalidReference)
    assert result.message == "Enter a GitHub repository."


def test_names_with_dots_and_hyphens_survive():
    assert parse_repo_reference("https://github.com/some-org/my.cool_repo-2") == RepoRef(
        owner="some-org", repo="my.cool_repo-2"
    )


# --- Opening a pull request ---------------------------------------------------
#
# Every branch here is one a run actually reaches at the end of its work, so
# none of them may raise: the commits already exist, and a pull request that
# could not be opened has to come back as something the run can put on the
# page rather than as a traceback out of a detached process.


class _Response:
    """Enough of an httpx response for these paths."""

    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


REF = RepoRef(owner="idm23", repo="workbench")


def _answers(monkeypatch, *, post=None, get=None):
    seen: dict = {}

    def fake_post(url, **kwargs):
        seen["post"] = {"url": url, **kwargs}
        return post

    def fake_get(url, **kwargs):
        seen["get"] = {"url": url, **kwargs}
        return get

    monkeypatch.setattr(github.httpx, "post", fake_post)
    monkeypatch.setattr(github.httpx, "get", fake_get)
    return seen


def _open(**overrides):
    kwargs = {
        "head": "workbench/task-16",
        "base": "staging",
        "title": "Starlette deprecation warning",
        "body": "what changed",
        "token": "pat",
    }
    return open_pull_request(REF, **{**kwargs, **overrides})


def test_a_created_pull_request_reports_where_it_is(monkeypatch):
    seen = _answers(monkeypatch, post=_Response(201, {"html_url": "https://gh/pr/7"}))

    result = _open()

    assert result == PullRequestOpened(url="https://gh/pr/7", created=True)
    assert seen["post"]["json"]["base"] == "staging"
    assert seen["post"]["headers"]["Authorization"] == "Bearer pat"


def test_a_duplicate_returns_the_pull_request_that_already_exists(monkeypatch):
    """A second execute run on the same task pushes more commits to the same
    branch, and GitHub refuses a second pull request for it. That is the right
    outcome reached the long way round, not a failure."""
    seen = _answers(
        monkeypatch,
        post=_Response(422, {"message": "A pull request already exists"}),
        get=_Response(200, [{"html_url": "https://gh/pr/7"}]),
    )

    result = _open()

    assert result == PullRequestOpened(url="https://gh/pr/7", created=False)
    assert seen["get"]["params"]["head"] == "idm23:workbench/task-16"


def test_an_invalid_base_branch_is_reported_as_such_not_as_a_duplicate(monkeypatch):
    """GitHub's real shape for "this base branch does not exist": a bare
    field/code pair with no `message`, distinct from the duplicate case's
    prose. Conflating the two is the actual bug this pins — a subtask whose
    base was never pushed to GitHub once got told its pull request was a
    duplicate when none had ever existed, because every 422 was assumed to
    mean that."""
    seen = _answers(
        monkeypatch,
        post=_Response(
            422,
            {
                "message": "Validation Failed",
                "errors": [{"resource": "PullRequest", "field": "base", "code": "invalid"}],
            },
        ),
    )

    result = _open()

    assert isinstance(result, PullRequestFailed)
    assert "base: invalid" in result.message
    assert "staging" in result.message  # names which base was rejected
    assert "get" not in seen  # never looks up an "existing" PR that isn't one


def test_a_duplicate_named_only_in_an_error_entry_is_still_recognised(monkeypatch):
    """GitHub's real shape for a duplicate: the top-level `message` is the
    generic "Validation Failed", and the actual "already exists" wording is
    nested in one of the `errors` entries instead."""
    seen = _answers(
        monkeypatch,
        post=_Response(
            422,
            {
                "message": "Validation Failed",
                "errors": [
                    {
                        "resource": "PullRequest",
                        "code": "custom",
                        "message": "A pull request already exists for idm23:workbench/task-16.",
                    }
                ],
            },
        ),
        get=_Response(200, [{"html_url": "https://gh/pr/7"}]),
    )

    result = _open()

    assert result == PullRequestOpened(url="https://gh/pr/7", created=False)
    assert "get" in seen


def test_a_422_with_nothing_open_is_a_failure_not_a_silent_success(monkeypatch):
    """A message that does say "already exists" is still trusted as a
    duplicate — but one with nothing actually open for it is a failure, not a
    silent success. Reporting that as success would leave a run claiming a
    pull request nobody can find."""
    _answers(
        monkeypatch,
        post=_Response(422, {"message": "A pull request already exists, apparently"}),
        get=_Response(200, []),
    )

    result = _open()

    assert isinstance(result, PullRequestFailed)


def test_a_refused_token_names_the_scope_rather_than_the_status(monkeypatch):
    _answers(monkeypatch, post=_Response(403))

    result = _open()

    assert isinstance(result, PullRequestFailed)
    assert "WORKBENCH_GITHUB_TOKEN" in result.message


def test_an_unreachable_github_is_reported_not_raised(monkeypatch):
    def explode(*_args, **_kwargs):
        raise github.httpx.ConnectError("no route to host")

    monkeypatch.setattr(github.httpx, "post", explode)

    result = _open()

    assert isinstance(result, PullRequestFailed)


def test_a_created_pull_request_without_a_url_is_not_reported_as_opened(monkeypatch):
    """GitHub said 201 and did not say where. Better to admit that than to
    record None as the run's pull request."""
    _answers(monkeypatch, post=_Response(201, {}))

    assert isinstance(_open(), PullRequestFailed)
