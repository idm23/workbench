"""Turning model-authored prose into HTML.

A run's `plan` and `summary`, and the `text`/`thinking` events it streams,
are Markdown — the same style Claude Code's own CLI produces — because
nothing in `agents/prompts.py` asks for a format and that is what the model
writes unprompted. Until now the page showed that source verbatim inside a
`white-space: pre-wrap` block, so a heading came through as a literal `#` and
a fenced code block as a literal ` ``` `. This renders it instead.

Deliberately narrow: only *complete* text is ever passed through
`render_markdown` — a plan or summary in full, a finished event's body, a
subtask's own `body`. The character-truncated previews in the task tree
(`TaskNode.body_preview`, `ReadyTask.plan_preview`) are cut at an arbitrary
byte offset and can land mid-construct (mid code-fence, mid `**bold`), so
they stay plain text; rendering a fragment as Markdown risks producing a
disclosure triangle's one-line teaser as a stray heading or an unclosed
code block, which is worse than the raw syntax it replaces. The `_rest` half
revealed by expanding one, and every other full field, goes through this.

This is the only module that marks a string `Markup`-safe. Nothing else
should get to decide that model output is safe to render unescaped — that is
exactly the judgement `nh3.clean` below exists to make instead of a person.
"""

import nh3
from markdown_it import MarkdownIt
from markupsafe import Markup

#: `breaks=True` turns a single newline into `<br>` rather than requiring a
#: blank line between paragraphs. Chat-style prose (this project's plans and
#: summaries) is written the way a terminal renders it, with single line
#: breaks between sentences that were never meant to run together — strict
#: CommonMark would collapse those into one paragraph.
#:
#: `html=False`: raw HTML in the source is escaped to literal text rather
#: than passed through, so the sanitizer below only ever has to reason about
#: tags Markdown syntax itself produced, not whatever the model typed.
_renderer = MarkdownIt("commonmark", {"breaks": True, "html": False})

#: What a plan or summary is allowed to render as. Prose formatting and
#: structure, nothing that executes or reaches off the page — no `<img>`
#: (an agent narrating a fetched URL should not cause a request), no
#: `<table>` (not part of the CommonMark this renders anyway), no attributes
#: beyond a link's own `href`/`title`.
_ALLOWED_TAGS = {
    "p",
    "br",
    "hr",
    "strong",
    "em",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "a",
}
_ALLOWED_ATTRIBUTES = {"a": {"href", "title"}}
#: Excludes `javascript:` and friends. `mailto:` is kept because a plan
#: crediting or CC-ing someone by address is plausible; nothing else the
#: model would write a link to needs a scheme beyond these.
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}


def render_markdown(text: str | None) -> Markup:
    """`text` rendered from Markdown to sanitized, `Markup`-safe HTML.

    `None` or empty input renders as empty `Markup` rather than raising, so a
    template can pipe an optional field straight through this filter without
    its own `{% if %}` guard.
    """
    if not text:
        return Markup("")

    html = _renderer.render(text)
    clean = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    )
    return Markup(clean)
