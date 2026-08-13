"""Reference parsing.

The parser is the fiddly part and runs on every add, so it is worth pinning
down. The HTTP lookup is not mocked — at this size the cost of maintaining
fakes outweighs what they would catch.
"""

import pytest

from workbench.github import InvalidReference, RepoRef, parse_repo_reference


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
