"""The preview/fold split that keeps a long event payload from filling the
page — see the module docstring in `runs/render.py` for why this exists
twice (here, and again in `run_detail.html`'s own `<script>`)."""

from workbench.database.models import RunEventKind
from workbench.runs.render import PREVIEW_CHARS, PREVIEW_LINES, event_body


def test_text_never_folds_regardless_of_length():
    body = event_body(RunEventKind.TEXT, {"text": "line\n" * 500})

    assert body.rest is None
    assert body.preview == "line\n" * 500


def test_thinking_never_folds_regardless_of_length():
    body = event_body(RunEventKind.THINKING, {"text": "x" * 5000})

    assert body.rest is None
    assert body.preview == "x" * 5000


def test_status_is_shown_whole():
    body = event_body(RunEventKind.STATUS, {"status": "running"})

    assert body.preview == "running"
    assert body.rest is None


def test_input_never_folds_regardless_of_length():
    body = event_body(RunEventKind.INPUT, {"text": "please " * 200})

    assert body.rest is None


def test_a_short_tool_result_is_shown_in_full():
    body = event_body(RunEventKind.TOOL_RESULT, {"text": "ok", "is_error": False})

    assert body.preview == "ok"
    assert body.rest is None


def test_a_long_tool_result_folds():
    text = "\n".join(f"line {i}" for i in range(200))

    body = event_body(RunEventKind.TOOL_RESULT, {"text": text, "is_error": False})

    assert body.rest is not None
    assert body.preview.count("\n") < PREVIEW_LINES
    # Concatenating what is shown and what is folded reconstructs the text.
    assert text == body.preview + "\n" + body.rest


def test_a_tool_result_with_a_single_very_long_line_still_folds():
    """A payload need not have many lines to be worth folding — one huge
    line should hit the character bound instead."""
    text = "x" * (PREVIEW_CHARS * 5)

    body = event_body(RunEventKind.TOOL_RESULT, {"text": text, "is_error": False})

    assert body.rest is not None
    assert len(body.preview) <= PREVIEW_CHARS


def test_tool_use_with_no_input_shows_only_the_name():
    body = event_body(RunEventKind.TOOL_USE, {"id": "1", "name": "Bash", "input": {}})

    assert body.preview == "Bash"
    assert body.rest is None


def test_tool_use_with_a_small_input_is_shown_in_full():
    body = event_body(
        RunEventKind.TOOL_USE, {"id": "1", "name": "Bash", "input": {"command": "ls"}}
    )

    assert "Bash" in body.preview
    assert "ls" in body.preview
    assert body.rest is None


def test_tool_use_with_a_large_input_folds_and_keeps_the_name_in_the_preview():
    """The case named in the task: a Write's whole file content, in
    `input.content`, should not fill the page."""
    big_input = {"file_path": "api.py", "content": "x" * 5000}

    body = event_body(RunEventKind.TOOL_USE, {"id": "1", "name": "Write", "input": big_input})

    assert body.rest is not None
    assert body.preview.startswith("Write")


def test_a_notice_with_no_data_is_shown_in_full():
    body = event_body(RunEventKind.NOTICE, {"text": "System: init"})

    assert body.preview == "System: init"
    assert body.rest is None


def test_a_notice_with_small_data_is_shown_in_full():
    body = event_body(RunEventKind.NOTICE, {"text": "System: config", "data": {"a": 1}})

    assert body.rest is None
    assert "System: config" in body.preview
    assert '"a": 1' in body.preview


def test_a_notice_with_large_data_folds():
    body = event_body(
        RunEventKind.NOTICE,
        {"text": "System: config", "data": {"blob": "y" * 5000}},
    )

    assert body.rest is not None
    assert body.preview.startswith("System: config")


def test_a_notice_rate_limit_reading_is_not_echoed_into_the_body():
    """`rate_limit` is already surfaced on every page via the rate-limit
    panel (CLAUDE.md) — repeating its structure inline would be noise, so
    only `text` and `data` feed the body."""
    body = event_body(
        RunEventKind.NOTICE,
        {
            "text": "Rate limit approaching (five_hour).",
            "rate_limit": {"status": "approaching", "type": "five_hour"},
        },
    )

    assert body.preview == "Rate limit approaching (five_hour)."
    assert body.rest is None


def test_a_malformed_payload_degrades_to_empty_rather_than_raising():
    assert event_body(RunEventKind.TEXT, {}).preview == ""
    assert event_body(RunEventKind.TOOL_USE, {}).preview == ""
    assert event_body(RunEventKind.TOOL_RESULT, {}).preview == ""
    assert event_body(RunEventKind.NOTICE, {}).preview == ""
