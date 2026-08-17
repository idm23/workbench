"""Reference parsing, and the credential handling around pushing.

The parser is the fiddly part and runs on every add, so it is worth pinning
down. The HTTP lookup is not mocked — at this size the cost of maintaining
fakes outweighs what they would catch. Redaction is tested because the failure
mode is a token written into the database and rendered on a page.
"""

import pytest

from workbench.github import (
    InvalidReference,
    PushFailed,
    RepoRef,
    credentials_missing,
    parse_repo_reference,
    push_branch,
    redact,
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


def test_no_token_configured_is_reported_before_a_run_starts(monkeypatch):
    """The reason has to reach the page before an agent spends minutes working."""
    monkeypatch.delenv("WORKBENCH_GITHUB_TOKEN", raising=False)

    reason = credentials_missing()

    assert reason is not None
    assert "WORKBENCH_GITHUB_TOKEN" in reason


def test_a_configured_token_clears_the_warning(monkeypatch):
    monkeypatch.setenv("WORKBENCH_GITHUB_TOKEN", "ghp_example")

    assert credentials_missing() is None


def test_whitespace_only_token_counts_as_missing(monkeypatch):
    """An env file with `WORKBENCH_GITHUB_TOKEN=` must not look configured."""
    monkeypatch.setenv("WORKBENCH_GITHUB_TOKEN", "   ")

    assert credentials_missing() is not None


def test_redact_removes_the_token_from_git_output(monkeypatch):
    """git echoes the push URL on failure, and that URL contains the token."""
    monkeypatch.setenv("WORKBENCH_GITHUB_TOKEN", "ghp_secret")

    message = redact(
        "fatal: could not read from 'https://x-access-token:ghp_secret@github.com/o/r.git'"
    )

    assert "ghp_secret" not in message
    assert "***" in message


def test_redact_is_a_no_op_without_a_token(monkeypatch):
    monkeypatch.delenv("WORKBENCH_GITHUB_TOKEN", raising=False)

    assert redact("nothing to hide") == "nothing to hide"


def test_push_without_a_token_fails_rather_than_running_git(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKBENCH_GITHUB_TOKEN", raising=False)

    result = push_branch(tmp_path, RepoRef("idm23", "workbench"), "workbench/task-1-x")

    assert isinstance(result, PushFailed)
