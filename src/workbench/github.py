"""Parsing and lookup of GitHub repository references.

Both entry points return a result object rather than raising. The caller
discriminates with `isinstance`, which a type checker verifies exhaustively,
instead of depending on the caller remembering which exceptions to catch and
what each one means.
"""

import re
from dataclasses import dataclass

import httpx

API_ROOT = "https://api.github.com"
TIMEOUT_SECONDS = 5.0

# GitHub logins are alphanumeric with interior hyphens; repository names also
# allow dots and underscores.
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_REPO = r"[A-Za-z0-9_.-]+"

_PATTERNS = (
    re.compile(rf"^https?://(?:www\.)?github\.com/(?P<owner>{_OWNER})/(?P<repo>{_REPO})$"),
    re.compile(rf"^git@github\.com:(?P<owner>{_OWNER})/(?P<repo>{_REPO})$"),
    re.compile(rf"^(?P<owner>{_OWNER})/(?P<repo>{_REPO})$"),
)


@dataclass(frozen=True)
class RepoRef:
    """A successfully parsed owner/repo pair."""

    owner: str
    repo: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class InvalidReference:
    """The text supplied does not name a GitHub repository."""

    message: str


type ParsedReference = RepoRef | InvalidReference


@dataclass(frozen=True)
class RepoMetadata:
    """GitHub answered and the repository exists."""

    description: str | None
    default_branch: str | None


@dataclass(frozen=True)
class RepoNotFound:
    """GitHub answered, and the repository is not there or is not visible."""

    slug: str
    message: str


@dataclass(frozen=True)
class RepoLookupUnavailable:
    """GitHub could not be asked — rate limited, unreachable, or erroring.

    Distinct from RepoNotFound because it says nothing about whether the
    repository exists, so the caller can still save the project.
    """

    message: str


type RepoLookup = RepoMetadata | RepoNotFound | RepoLookupUnavailable


def parse_repo_reference(raw: str) -> ParsedReference:
    """Turn user input into an owner/repo pair.

    Accepts a full HTTPS URL, an SSH remote, or bare `owner/repo` — the last
    because this is mostly typed on a phone, where less typing matters.
    """
    text = raw.strip().removesuffix("/")
    text = text.removesuffix(".git")

    if not text:
        return InvalidReference("Enter a GitHub repository.")

    for pattern in _PATTERNS:
        match = pattern.match(text)
        if match:
            return RepoRef(owner=match["owner"], repo=match["repo"])

    return InvalidReference(
        f"{raw.strip()!r} is not a GitHub repository. Expected something like "
        "https://github.com/owner/repo or owner/repo."
    )


def fetch_repo_metadata(ref: RepoRef) -> RepoLookup:
    """Look the repository up on GitHub.

    Rate limiting is an expected outcome rather than an exceptional one: this
    is unauthenticated and capped at 60 requests an hour, so it is reported as
    RepoLookupUnavailable and left to the caller to decide about.
    """
    try:
        response = httpx.get(
            f"{API_ROOT}/repos/{ref.owner}/{ref.repo}",
            headers={
                "Accept": "application/vnd.github+json",
                # GitHub rejects requests without a User-Agent.
                "User-Agent": "workbench",
            },
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return RepoLookupUnavailable(
            f"Could not reach GitHub to look up {ref.slug}."
        )

    if response.status_code == 404:
        return RepoNotFound(
            slug=ref.slug,
            message=(
                f"GitHub has no repository {ref.slug}. Check the spelling — "
                "or it may be private, which this cannot see yet."
            ),
        )

    if response.status_code != 200:
        return RepoLookupUnavailable(
            f"GitHub returned {response.status_code} for {ref.slug} "
            "(most likely the unauthenticated rate limit)."
        )

    payload = response.json()
    return RepoMetadata(
        description=payload.get("description"),
        default_branch=payload.get("default_branch"),
    )
