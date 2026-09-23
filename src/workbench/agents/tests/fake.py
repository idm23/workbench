"""A backend that runs a script instead of a model.

Every test above the seam — this package's, and the runner's when it arrives —
needs a backend that produces a known sequence of events without a credential,
a subprocess, or a bill. That is this. It also serves as the check that the
protocol is implementable by something that is not Claude, which is the whole
claim the seam is making.
"""

from collections.abc import Sequence

from workbench.agents.protocol import (
    CREDENTIAL_SUBSCRIPTION,
    AgentEvent,
    AgentFinished,
    AgentOutcome,
    AgentRequest,
    AgentStream,
    CredentialStatus,
)


class FakeBackend:
    """Yields the events it was given, then the outcome it was given.

    Records the requests it received, so a test can assert on what the caller
    asked for — the worktree, the phase, and whether a resume token was
    carried across from an earlier run.
    """

    def __init__(
        self,
        events: Sequence[AgentEvent] = (),
        outcome: AgentOutcome | None = None,
        name: str = "fake",
        billing_notice: str = "billing nothing",
        credential: CredentialStatus | None = None,
    ) -> None:
        self._events = list(events)
        self._outcome = outcome or AgentFinished(text="done", resume_token="fake-session")
        self._name = name
        #: Settable, because the interesting cases for anything reading this
        #: are the unhappy ones, and a fake that could only be authenticated
        #: would be useless to exactly the tests that need it.
        self.credential = credential or CredentialStatus(
            backend=name,
            logged_in=True,
            method=CREDENTIAL_SUBSCRIPTION,
            account="fake@example.com",
            detail="Signed in as fake@example.com, billing a Claude subscription.",
        )
        self._billing_notice = billing_notice
        self.requests: list[AgentRequest] = []

    @property
    def wants_endpoint(self) -> bool:
        """No: a fake talks to nothing, so a worker node's URL means nothing
        to it. Matches the hosted backends, which is the case worth defaulting
        to — see `Backend.wants_endpoint`."""
        return False

    @property
    def name(self) -> str:
        return self._name

    @property
    def billing_notice(self) -> str:
        return self._billing_notice

    def credential_status(self) -> CredentialStatus:
        return self.credential

    async def run(self, request: AgentRequest) -> AgentStream:
        self.requests.append(request)
        for event in self._events:
            yield event
        yield self._outcome
