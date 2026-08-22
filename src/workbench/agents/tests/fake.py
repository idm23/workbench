"""A backend that runs a script instead of a model.

Every test above the seam — this package's, and the runner's when it arrives —
needs a backend that produces a known sequence of events without a credential,
a subprocess, or a bill. That is this. It also serves as the check that the
protocol is implementable by something that is not Claude, which is the whole
claim the seam is making.
"""

from collections.abc import Sequence

from workbench.agents.protocol import (
    AgentEvent,
    AgentFinished,
    AgentOutcome,
    AgentRequest,
    AgentStream,
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
    ) -> None:
        self._events = list(events)
        self._outcome = outcome or AgentFinished(text="done", resume_token="fake-session")
        self._name = name
        self.requests: list[AgentRequest] = []

    @property
    def name(self) -> str:
        return self._name

    async def run(self, request: AgentRequest) -> AgentStream:
        self.requests.append(request)
        for event in self._events:
            yield event
        yield self._outcome
