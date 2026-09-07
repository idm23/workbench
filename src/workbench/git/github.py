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

#: Opening a pull request is a write against a repository that may be large,
#: and it happens once at the end of a run rather than on a page load, so it
#: gets more room than the lookup above.
WRITE_TIMEOUT_SECONDS = 30.0

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
    def ssh_url(self) -> str:
        """The form that authenticates with a deploy key rather than a password.

        Only ever used for pushing. Fetching stays on `url`, because a public
        repository clones and fetches with no credentials at all — requiring a
        key to read would break adding a project on a machine that has none.
        """
        return f"git@github.com:{self.owner}/{self.repo}.git"

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
class PullRequestOpened:
    """A pull request exists for this branch — new, or already there."""

    url: str

    #: False when the pull request was already open. A re-run of an execute
    #: phase pushes more commits to the same branch, and GitHub refuses a
    #: second pull request for it; that is success, not failure, so the
    #: distinction is recorded rather than the outcome changed.
    created: bool = True


@dataclass(frozen=True)
class PullRequestFailed:
    """No pull request, and a person has to know why.

    Not raised, for the same reason as everything else in this module: the
    caller is a run that has already done its work, and losing that work over
    a failed API call would be the worse outcome.
    """

    message: str


type PullRequestResult = PullRequestOpened | PullRequestFailed


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        # GitHub rejects requests without a User-Agent.
        "User-Agent": "workbench",
    }


def _validation_detail(payload: object) -> str:
    """A readable explanation from a 422's body.

    GitHub's `errors` entries carry a `message` for some failures (a
    duplicate pull request) and only `field`/`code` for others (an invalid
    base branch) — this reads whichever is there rather than assuming the
    shape that happens to matter for the one case already handled.
    """
    if not isinstance(payload, dict):
        return "GitHub did not explain why."
    message = payload.get("message") or ""
    raw_errors = payload.get("errors")
    errors = raw_errors if isinstance(raw_errors, list) else []
    parts = [
        error.get("message") or f"{error.get('field', '?')}: {error.get('code', '?')}"
        for error in errors
        if isinstance(error, dict)
    ]
    detail = "; ".join(parts)
    combined = f"{message} {detail}".strip()
    return combined or "GitHub did not explain why."


def _is_duplicate_pull_request(detail: str) -> bool:
    """Whether a 422's explanation is GitHub's own wording for "this pull
    request already exists" rather than something else — an invalid base
    branch, most commonly, which reads as pure prose with no such phrase."""
    return "already exists" in detail.lower()


def _existing_pull_request(ref: RepoRef, head: str, token: str) -> PullRequestResult:
    """The open pull request for a branch, when one is already there.

    Asked only after a 422, which is what GitHub answers for a duplicate. The
    alternative — treating that as an error — would make the second execute
    run on a task look like a failure when it did exactly the right thing.
    """
    try:
        response = httpx.get(
            f"{API_ROOT}/repos/{ref.owner}/{ref.repo}/pulls",
            params={"head": f"{ref.owner}:{head}", "state": "open"},
            headers=_headers(token),
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        return PullRequestFailed(f"Could not reach GitHub to look up the pull request for {head}.")

    if response.status_code != 200:
        return PullRequestFailed(
            f"GitHub returned {response.status_code} looking up the pull request for {head}."
        )

    payload = response.json()
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        url = payload[0].get("html_url")
        if isinstance(url, str):
            return PullRequestOpened(url=url, created=False)

    return PullRequestFailed(
        f"GitHub refused a pull request for {head} as a duplicate, but reports none open for it."
    )


def open_pull_request(
    ref: RepoRef, *, head: str, base: str, title: str, body: str, token: str
) -> PullRequestResult:
    """Open a pull request from a task's branch onto what it was cut from.

    `base` is the branch the worktree was created from rather than the
    repository's default. That is what sends work into `staging` on a project
    that promotes through it, and what makes a task branched from another
    task stack onto that one instead of jumping the queue to main.
    """
    try:
        response = httpx.post(
            f"{API_ROOT}/repos/{ref.owner}/{ref.repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
            headers=_headers(token),
            timeout=WRITE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        return PullRequestFailed(f"Could not reach GitHub to open a pull request for {head}.")

    if response.status_code == 201:
        url = response.json().get("html_url")
        if isinstance(url, str):
            return PullRequestOpened(url=url)
        return PullRequestFailed("GitHub opened a pull request but did not say where.")

    if response.status_code == 422:
        # Either a duplicate, or something genuinely wrong with the request —
        # a base branch that does not exist, most likely. Only the first is
        # recoverable, and reading GitHub's own explanation is how they are
        # told apart, rather than assuming every 422 is the recoverable one:
        # that assumption is what once turned "the base branch was never
        # pushed" into a notice claiming a duplicate PR existed when none did.
        detail = _validation_detail(response.json())
        if _is_duplicate_pull_request(detail):
            return _existing_pull_request(ref, head, token)
        return PullRequestFailed(
            f"GitHub rejected the pull request for {head} onto {base}: {detail}"
        )

    if response.status_code in (401, 403):
        return PullRequestFailed(
            f"GitHub refused the pull request for {head} with {response.status_code} — "
            "WORKBENCH_GITHUB_TOKEN is missing the pull-request write scope for this "
            "repository, or has expired."
        )

    return PullRequestFailed(
        f"GitHub returned {response.status_code} opening a pull request for {head}."
    )
