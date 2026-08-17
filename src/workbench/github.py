"""Parsing and lookup of GitHub repository references.

Both entry points return a result object rather than raising. The caller
discriminates with `isinstance`, which a type checker verifies exhaustively,
instead of depending on the caller remembering which exceptions to catch and
what each one means.
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from workbench.config import github_token

logger = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
TIMEOUT_SECONDS = 5.0

#: Pushing a branch is slower than an API call and involves the network twice.
PUSH_TIMEOUT_SECONDS = 300

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


def _headers() -> dict[str, str]:
    """Standard headers, authenticated when a token is configured.

    A token raises the rate limit from 60 requests an hour to 5,000 and makes
    private repositories visible, so lookups quietly improve once one is set
    without any other code path changing.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        # GitHub rejects requests without a User-Agent.
        "User-Agent": "workbench",
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def redact(text: str) -> str:
    """Remove the GitHub token from anything about to be logged or stored.

    The token appears in push URLs, so git's own error output can contain it.
    Everything from git goes through here before it reaches the database or the
    journal.
    """
    token = github_token()
    if not token:
        return text
    return text.replace(token, "***")


def fetch_repo_metadata(ref: RepoRef) -> RepoLookup:
    """Look the repository up on GitHub.

    Rate limiting is an expected outcome rather than an exceptional one: when
    unauthenticated this is capped at 60 requests an hour, so it is reported as
    RepoLookupUnavailable and left to the caller to decide about.
    """
    try:
        response = httpx.get(
            f"{API_ROOT}/repos/{ref.owner}/{ref.repo}",
            headers=_headers(),
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return RepoLookupUnavailable(f"Could not reach GitHub to look up {ref.slug}.")

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


@dataclass(frozen=True)
class Pushed:
    """The branch reached GitHub."""

    branch: str


@dataclass(frozen=True)
class PushFailed:
    """The branch did not reach GitHub. Message is safe to display."""

    message: str


type PushResult = Pushed | PushFailed


@dataclass(frozen=True)
class PullRequestOpened:
    url: str
    number: int


@dataclass(frozen=True)
class PullRequestFailed:
    """No pull request exists. The work is still pushed and recoverable."""

    message: str


type PullRequestResult = PullRequestOpened | PullRequestFailed


def credentials_missing() -> str | None:
    """Why pushing is unavailable, or None if it is available.

    Called before starting a run so the reason appears on screen up front,
    rather than after an agent has spent several minutes doing the work.
    """
    if github_token() is None:
        return (
            "No GitHub token configured, so the branch cannot be pushed and no "
            "pull request can be opened. Set WORKBENCH_GITHUB_TOKEN in "
            "/etc/workbench/env."
        )
    return None


def push_branch(worktree: Path, ref: RepoRef, branch: str) -> PushResult:
    """Push the task's branch to GitHub.

    The token goes in the remote URL for this one invocation rather than into
    `.git/config` or a credential helper, so it is never written to disk in the
    worktree. It does appear in this process's argv, which is visible to other
    processes on the machine — acceptable only because the agent running as the
    same user can read the token from the environment anyway.
    """
    token = github_token()
    if token is None:
        return PushFailed("No GitHub token configured.")

    url = f"https://x-access-token:{token}@github.com/{ref.owner}/{ref.repo}.git"
    try:
        completed = subprocess.run(
            ["git", "push", "--force-with-lease", url, f"HEAD:refs/heads/{branch}"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=PUSH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return PushFailed(f"Pushing {branch} timed out after {PUSH_TIMEOUT_SECONDS}s.")

    if completed.returncode != 0:
        # git echoes the remote URL on failure, token included.
        return PushFailed(redact(completed.stderr.strip())[-1000:] or "git push failed.")

    return Pushed(branch)


def open_pull_request(
    ref: RepoRef,
    branch: str,
    base: str,
    title: str,
    body: str,
) -> PullRequestResult:
    """Open a pull request for a pushed branch.

    An existing open pull request for the same branch is returned rather than
    treated as an error, so re-running the execute phase after a failure later
    in the sequence does not dead-end on "a pull request already exists".
    """
    token = github_token()
    if token is None:
        return PullRequestFailed("No GitHub token configured.")

    try:
        response = httpx.post(
            f"{API_ROOT}/repos/{ref.owner}/{ref.repo}/pulls",
            headers=_headers(),
            json={
                "title": title[:250],
                "head": branch,
                "base": base,
                # GitHub caps bodies at 65,536 characters and rejects the whole
                # request when exceeded, which would throw away a finished run.
                "body": body[:60000],
            },
            timeout=TIMEOUT_SECONDS * 4,
            follow_redirects=True,
        )
    except httpx.HTTPError as error:
        return PullRequestFailed(f"Could not reach GitHub to open a pull request: {error}")

    if response.status_code == 201:
        payload = response.json()
        return PullRequestOpened(url=payload["html_url"], number=payload["number"])

    if response.status_code == 422:
        existing = _find_open_pull_request(ref, branch)
        if existing is not None:
            return existing

    return PullRequestFailed(
        f"GitHub returned {response.status_code} opening a pull request for {branch}. "
        "The branch is pushed, so the pull request can be opened by hand."
    )


def _find_open_pull_request(ref: RepoRef, branch: str) -> PullRequestOpened | None:
    """The already-open pull request for a branch, if there is one."""
    try:
        response = httpx.get(
            f"{API_ROOT}/repos/{ref.owner}/{ref.repo}/pulls",
            headers=_headers(),
            params={"head": f"{ref.owner}:{branch}", "state": "open"},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return None

    if response.status_code != 200:
        return None

    matches = response.json()
    if not matches:
        return None
    return PullRequestOpened(url=matches[0]["html_url"], number=matches[0]["number"])
