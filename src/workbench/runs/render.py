"""How one run event's payload becomes what a person reads.

Split from the JSON-serialisable `payload` dict at *render* time rather than
at translation time (`agents/claude.py`), because those are different bounds
for different reasons. `MAX_PAYLOAD_CHARS` there bounds a table kept forever
— 10,000 characters is a legitimate amount of history to keep. This bounds
what renders inline on a page read from a phone, where a run doing real work
emits dozens of these events and a single `Read`'s worth of file content
should not bury everything around it.

`run_detail.html` renders events in two places — the server-rendered
`{% for event in events %}` loop for anything already committed, and the
`add()` function its own `<script>` block calls for anything the SSE stream
delivers live. Both need the same preview/fold decision, so it exists twice:
this module for the Jinja side (registered as a template global in `app.py`),
and a hand-written mirror inside `run_detail.html`'s `<script>` for the JS
side. Keep the two in the same shape when either changes — that symmetry is
the whole point, not an incidental duplication.
"""

import json
from dataclasses import dataclass
from typing import Any

from workbench.database.models import RunEventKind

#: How much of an event shows before folding. A page read from a phone wants
#: a glance, not a screenful — judgement call, not a measured optimum. Two
#: bounds because a payload can be a handful of very long lines just as
#: easily as many short ones.
PREVIEW_LINES = 6
PREVIEW_CHARS = 480

#: Kinds whose payload can carry a whole clipped blob — a tool's full output,
#: a Write's file contents, a system message's raw data — read only
#: occasionally and in detail rather than skimmed every time. `text` and
#: `thinking` are the agent talking and stay open regardless of length;
#: `status` and `input` are already short by construction.
FOLDABLE_KINDS = frozenset({RunEventKind.TOOL_USE, RunEventKind.TOOL_RESULT, RunEventKind.NOTICE})


@dataclass(frozen=True)
class EventBody:
    """What to show, and what to fold behind a `<details>`.

    `rest` is `None` for a kind that never folds, or for one short enough to
    show in full — which is also the template's cue not to render a
    `<details>` at all, the same contract `tasks.tree.TaskNode.body_rest`
    already uses for task bodies.
    """

    preview: str
    rest: str | None = None


def _body_text(kind: RunEventKind, payload: dict[str, Any]) -> str:
    """The full text a kind's payload renders as, before any folding.

    Every access goes through `.get()` with a default: a malformed or
    older-shaped row degrades to a plain or empty render rather than a
    broken page, in the same spirit as `agents/claude.py` never raising on
    an ordinary condition.
    """
    if kind is RunEventKind.TOOL_USE:
        name = str(payload.get("name") or "")
        tool_input = payload.get("input")
        if not tool_input:
            return name
        rendered = json.dumps(tool_input, indent=2, ensure_ascii=False)
        return f"{name}\n{rendered}" if name else rendered

    if kind is RunEventKind.NOTICE:
        text = str(payload.get("text") or "")
        # `rate_limit` is left alone even though it is structured data too —
        # it is already surfaced on every page via the rate-limit panel
        # (CLAUDE.md), so repeating it inline would just be noise. `data` is
        # not shown anywhere else, so it is the one worth folding in.
        data = payload.get("data")
        if not data:
            return text
        rendered = json.dumps(data, indent=2, ensure_ascii=False)
        return f"{text}\n{rendered}" if text else rendered

    # text, thinking, status, input — today's fallback, unchanged.
    return str(payload.get("text") or payload.get("status") or "")


def _split(text: str) -> tuple[str, str | None]:
    """`text` cut to a skimmable preview, and whatever was left out.

    Cuts at `PREVIEW_LINES` lines, further bounded to `PREVIEW_CHARS` so a
    handful of very long lines still folds. Returns `(text, None)` unchanged
    when the cut would not actually shorten it.
    """
    lines = text.splitlines()
    head = "\n".join(lines[:PREVIEW_LINES])
    if len(head) > PREVIEW_CHARS:
        head = head[:PREVIEW_CHARS]
    if len(head) >= len(text):
        return text, None
    rest = text[len(head) :].lstrip("\n")
    return head, (rest or None)


def event_body(kind: RunEventKind, payload: dict[str, Any]) -> EventBody:
    """The preview to show for one event, and its fold-behind-`<details>` rest.

    Called from `run_detail.html` as a Jinja global (registered in
    `app.py`) — see the module docstring for why a JS twin also exists.
    """
    text = _body_text(kind, payload)
    if kind not in FOLDABLE_KINDS:
        return EventBody(preview=text, rest=None)
    preview, rest = _split(text)
    return EventBody(preview=preview, rest=rest)
