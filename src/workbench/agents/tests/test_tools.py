"""The tools a local model is handed, and the two things they must never do.

Most of what is here is ordinary — read a file, edit it, run a command — and
tested because a small model leans on the error messages to correct itself, so
the wording is part of the interface rather than decoration.

The two that are not ordinary: nothing may touch a path outside the worktree,
and the plan phase must not be able to reach a tool that writes even if it
asks for one by name.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from workbench.agents.tools import (
    _ALIASES,
    MAX_OUTPUT_CHARS,
    READ_ONLY_TOOLS,
    PlanSubmitted,
    ToolContext,
    ToolResult,
    dispatch,
    tool_names_for,
    tools_for,
)
from workbench.database.models import RunPhase


@pytest.fixture
def worktree(tmp_path) -> Path:
    tree = tmp_path / "worktree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (tree / "README.md").write_text("# Project\n", encoding="utf-8")
    (tree / ".git").mkdir()
    (tree / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return tree


@pytest.fixture
def context(worktree) -> ToolContext:
    return ToolContext(worktree=worktree, api_base="http://127.0.0.1:8787", run_id=7, task_id=3)


def call(context: ToolContext, name: str, phase: RunPhase = RunPhase.EXECUTE, **args: Any):
    """Dispatch one call that is expected to answer, rather than end the run.

    `submit_plan` is the only tool that does not, and the tests for it call
    `dispatch` directly — so narrowing here keeps every other test reading as
    one line instead of two.
    """
    result = dispatch(phase, name, args, context)
    assert isinstance(result, ToolResult), f"{name} ended the run: {result!r}"
    return result


def test_reading_a_file_numbers_its_lines(context):
    """`edit_file` matches on text, so a model that can cite a line number is
    one that can quote the right text back."""
    result = call(context, "read_file", path="src/app.py")

    assert isinstance(result, ToolResult)
    assert not result.is_error
    assert "1│def main():" in result.text


def test_reading_takes_a_window(context):
    result = call(context, "read_file", path="src/app.py", offset=2, limit=1)

    assert "2│    return 1" in result.text
    assert "def main" not in result.text


def test_a_long_file_says_where_it_was_cut(context, worktree):
    """Text that stops with no marker is text the model treats as complete."""
    (worktree / "long.txt").write_text("\n".join(f"line {n}" for n in range(500)))

    result = call(context, "read_file", path="long.txt", limit=10)

    assert "more lines; read again with a later offset" in result.text


def test_output_is_bounded(context, worktree):
    """Every result is both a row kept forever and a slice of a small context
    window. The window is the tighter of the two."""
    (worktree / "huge.txt").write_text("x" * (MAX_OUTPUT_CHARS * 3))

    result = call(context, "read_file", path="huge.txt")

    assert len(result.text) < MAX_OUTPUT_CHARS * 2
    assert "truncated" in result.text


def test_listing_skips_the_git_directory(context):
    """Not correctness — the difference between a listing a 7B model can use
    and one buried under object files."""
    result = call(context, "list_files", path=".", depth=3)

    assert "src/app.py" in result.text
    assert ".git" not in result.text


def test_listing_skips_the_git_pointer_file_in_a_worktree(context, worktree):
    """Every worktree looks like this: `git worktree add` writes `.git` as a
    file pointing at the real gitdir, not as a directory. Filtering only
    directories put it at the top of every listing the agent asked for."""
    shutil.rmtree(worktree / ".git")
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/task-1\n")

    result = call(context, "list_files", path=".", depth=2)

    assert ".git" not in result.text
    assert "README.md" in result.text


def test_search_finds_matches_and_reports_none(context):
    found = call(context, "search", pattern="def main")
    missing = call(context, "search", pattern="def nonexistent_thing")

    assert "src/app.py" in found.text
    assert missing.text.startswith("No matches for 'def nonexistent_thing'.")
    assert not missing.is_error  # finding nothing is an answer, not a failure


def test_a_command_reports_its_exit_code(context):
    result = call(context, "run_command", command="echo hello")

    assert "[exit 0]" in result.text
    assert "hello" in result.text


def test_a_failing_command_is_information_not_a_tool_error(context):
    """A red test is an answer to the question the model asked. Marking it an
    error invites a retry loop over something that is legitimately failing."""
    result = call(context, "run_command", command="exit 3")

    assert "[exit 3]" in result.text
    assert not result.is_error


def test_a_command_runs_in_the_worktree(context, worktree):
    result = call(context, "run_command", command="pwd")

    assert str(worktree.resolve()) in result.text


def test_a_command_that_hangs_is_stopped(context):
    result = call(context, "run_command", command="sleep 5", timeout=1)

    assert result.is_error
    assert "timed out" in result.text


def test_writing_then_reading_a_file(context, worktree):
    written = call(context, "write_file", path="src/new.py", content="x = 1\n")

    assert not written.is_error
    assert (worktree / "src" / "new.py").read_text() == "x = 1\n"


def test_editing_replaces_exact_text(context, worktree):
    result = call(context, "edit_file", path="src/app.py", old_text="return 1", new_text="return 2")

    assert not result.is_error
    assert "return 2" in (worktree / "src" / "app.py").read_text()


def test_editing_text_that_is_not_there_says_to_quote_it_exactly(context):
    """The commonest failure of a small model, and it recovers when told."""
    result = call(context, "edit_file", path="src/app.py", old_text="return 9", new_text="x")

    assert result.is_error
    assert "quote it exactly" in result.text


def test_an_ambiguous_edit_is_refused_with_the_count(context, worktree):
    (worktree / "twice.py").write_text("a = 1\na = 1\n")

    result = call(context, "edit_file", path="twice.py", old_text="a = 1", new_text="a = 2")

    assert result.is_error
    assert "appears 2 times" in result.text
    assert (worktree / "twice.py").read_text() == "a = 1\na = 1\n"


def test_an_ambiguous_edit_goes_through_when_asked(context, worktree):
    (worktree / "twice.py").write_text("a = 1\na = 1\n")

    result = call(
        context, "edit_file", path="twice.py", old_text="a = 1", new_text="a = 2", replace_all=True
    )

    assert not result.is_error
    assert (worktree / "twice.py").read_text() == "a = 2\na = 2\n"


def test_a_path_outside_the_worktree_is_refused(context, tmp_path):
    """Not the security boundary — that is the unprivileged account — but a
    model that wanders into the deployment is a realistic failure of a small
    one, and refusing by construction is cheaper than noticing afterwards."""
    outside = call(context, "write_file", path="../escaped.py", content="x = 1")

    assert outside.is_error
    assert "outside the worktree" in outside.text
    assert not (tmp_path / "escaped.py").exists()


def test_an_absolute_path_outside_the_worktree_is_refused(context):
    result = call(context, "read_file", path="/etc/passwd")

    assert result.is_error
    assert "outside the worktree" in result.text


def test_a_symlink_out_of_the_worktree_is_refused(context, worktree, tmp_path):
    """`resolve()` before comparing, so `..` and a symlink are one case."""
    secret = tmp_path / "secret.txt"
    secret.write_text("shh")
    (worktree / "link.txt").symlink_to(secret)

    result = call(context, "read_file", path="link.txt")

    assert result.is_error


def test_the_plan_phase_is_given_no_tool_that_writes():
    """The Claude adapter gets read-only planning from the SDK's plan mode.
    Here it is the absence of the tools, which is the stronger guarantee: there
    is nothing to bypass."""
    offered = tool_names_for(RunPhase.PLAN)

    assert "run_command" not in offered
    assert "write_file" not in offered
    assert "edit_file" not in offered
    assert "submit_plan" in offered


def test_the_plan_phase_refuses_a_writing_tool_it_was_never_offered(context, worktree):
    """A model that hallucinates a tool must not get one because the
    dispatcher was more permissive than the schema it was handed."""
    result = dispatch(RunPhase.PLAN, "run_command", {"command": "touch escaped"}, context)

    assert isinstance(result, ToolResult)
    assert result.is_error
    assert "not available in this phase" in result.text
    assert not (worktree / "escaped").exists()


def test_an_unknown_tool_lists_what_there_is(context):
    result = call(context, "invent_a_tool")

    assert result.is_error
    assert "list_files" in result.text


def test_submitting_a_plan_ends_the_planning_run(context):
    call(context, "read_file", RunPhase.PLAN, path="src/app.py")

    result = dispatch(
        RunPhase.PLAN,
        "submit_plan",
        {
            "plan": "Add the endpoint, then a test.",
            "subtasks": [{"title": "Add it", "body": "In app.py", "ready_to_execute": True}],
        },
        context,
    )

    assert isinstance(result, PlanSubmitted)
    assert result.plan == "Add the endpoint, then a test."
    assert result.subtasks[0].title == "Add it"
    assert result.subtasks[0].ready_to_execute


def test_a_malformed_subtask_is_dropped_rather_than_crashing(context):
    """Defensive despite the schema, for the same reason the Claude adapter
    is: a small model deviates from what it was told to send."""
    call(context, "read_file", RunPhase.PLAN, path="src/app.py")

    result = dispatch(
        RunPhase.PLAN,
        "submit_plan",
        {"plan": "Do it", "subtasks": ["not an object", {"body": "no title"}]},
        context,
    )

    assert isinstance(result, PlanSubmitted)
    assert result.subtasks == []


def test_a_verdict_before_looking_at_anything_is_refused(context):
    """What the first real run against a 7B did on turn one: reported
    needs_replanning — "the specification is too vague" — having read no file,
    listed no directory and run no command. That is a reflex, not a
    judgement."""
    result = call(context, "report_outcome", outcome="needs_replanning", detail="too vague")

    assert result.is_error
    assert "has not looked at anything" in result.text


def test_a_verdict_after_looking_goes_through(context, monkeypatch):
    """A task that genuinely cannot be done is still reportable — after one
    look. The refusal is about order, not about the answer."""
    sent: dict[str, Any] = {}
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, json, timeout: (
            sent.update(json) or httpx.Response(204, request=httpx.Request("POST", url))
        ),
    )
    call(context, "read_file", path="src/app.py")

    result = call(context, "report_outcome", outcome="needs_replanning", detail="still vague")

    assert not result.is_error
    assert sent["outcome"] == "needs_replanning"


def test_a_plan_submitted_before_looking_at_anything_is_refused(context):
    """The same reflex in the other phase: a plan written from the task title
    alone is not a plan."""
    result = dispatch(RunPhase.PLAN, "submit_plan", {"plan": "Looks easy enough"}, context)

    assert isinstance(result, ToolResult)
    assert result.is_error


def test_an_empty_plan_is_refused(context):
    call(context, "read_file", RunPhase.PLAN, path="src/app.py")

    result = dispatch(RunPhase.PLAN, "submit_plan", {"plan": "  "}, context)

    assert isinstance(result, ToolResult)
    assert result.is_error


def test_reporting_an_outcome_posts_to_workbenchs_own_api(context, monkeypatch):
    """The same endpoint the Claude backend's skill curls, so the two backends
    converge on one code path above the seam."""
    seen: dict[str, Any] = {}

    def fake_post(url: str, json: dict, timeout: float):
        seen["url"] = url
        seen["json"] = json
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    call(context, "read_file", path="src/app.py")

    result = call(context, "report_outcome", outcome="finished", detail="all done")

    assert not result.is_error
    assert seen["url"] == "http://127.0.0.1:8787/api/runs/7/outcome"
    assert seen["json"] == {"outcome": "finished", "detail": "all done"}


def test_an_unknown_outcome_is_refused_before_it_is_sent(context, monkeypatch):
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: pytest.fail("an invalid outcome was sent anyway")
    )

    result = call(context, "report_outcome", outcome="probably_fine")

    assert result.is_error


def test_an_unreachable_workbench_does_not_end_the_run(context, monkeypatch):
    """The run still has its commits and its summary. Losing an HTTP call must
    not throw those away — an unreported run is already defined as one nobody
    assumes succeeded."""

    def refuse(*args, **kwargs):
        raise httpx.ConnectError("nothing listening")

    monkeypatch.setattr(httpx, "post", refuse)
    call(context, "read_file", path="src/app.py")

    result = call(context, "report_outcome", outcome="finished")

    assert result.is_error
    assert "do not retry more than once" in result.text


def test_a_crashing_tool_is_reported_rather_than_raised(context, monkeypatch):
    """A tool that throws is the model's problem to route around, not the
    run's to die of."""
    from workbench.agents import tools as tools_module

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setitem(
        tools_module.TOOLS,
        "read_file",
        tools_module.TOOLS["read_file"].__class__(
            name="read_file",
            description="",
            parameters={},
            handler=explode,
        ),
    )

    result = call(context, "read_file", path="src/app.py")

    assert result.is_error
    assert "boom" in result.text


def test_the_schemas_are_what_chat_completions_expects():
    """Sent verbatim to the endpoint, so a wrong shape is a run that fails at
    the first request with a message from someone else's server."""
    schemas = tools_for(RunPhase.EXECUTE)

    assert all(schema["type"] == "function" for schema in schemas)
    assert all("name" in schema["function"] for schema in schemas)
    # Serialisable, because that is what actually goes over the wire.
    assert json.loads(json.dumps(schemas))


def test_asking_a_question_reports_it_as_an_outcome(context, monkeypatch):
    """It rides the outcome API rather than a route of its own: a question is
    the agent saying how the run went, recorded live so it survives the
    process that asked it."""
    sent: dict[str, Any] = {}

    def fake_post(url: str, json: dict, timeout: float):
        sent.update(url=url, **json)
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    call(context, "read_file", path="src/app.py")

    result = call(context, "ask_user", question="Should this replace the old endpoint?")

    assert not result.is_error
    assert sent["outcome"] == "needs_answer"
    assert sent["detail"] == "Should this replace the old endpoint?"
    assert "Stop now" in result.text


def test_a_question_before_looking_at_anything_is_refused(context, monkeypatch):
    """Asking before reading is not a question, it is a reflex — and every one
    of them costs a person their attention."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("it asked anyway"))

    result = call(context, "ask_user", question="What do you want?")

    assert result.is_error
    assert "has not looked at anything" in result.text


def test_an_empty_question_is_refused(context, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("it asked anyway"))
    call(context, "read_file", path="src/app.py")

    assert call(context, "ask_user", question="   ").is_error


def test_a_plan_run_cannot_ask(context):
    """It could not act on the answer if it got one: the phase is read-only,
    and the plan's job is to name the fork clearly enough that the execute run
    knows what to ask about."""
    result = dispatch(RunPhase.PLAN, "ask_user", {"question": "Which one?"}, context)

    assert isinstance(result, ToolResult)
    assert result.is_error
    assert "not available in this phase" in result.text


# --- Names a model reaches for ------------------------------------------------


def test_open_file_is_understood_as_read_file(context):
    """gpt-oss's own spelling, asked for fourteen times in two planning runs."""
    result = call(
        context, "open_file", phase=RunPhase.PLAN, path="src/app.py", line_start=2, line_end=2
    )

    assert not result.is_error
    assert "2│    return 1" in result.text
    assert "def main" not in result.text


def test_view_file_is_understood_too(context):
    assert not call(context, "view_file", phase=RunPhase.PLAN, path="src/app.py").is_error


def test_every_alias_is_read_only():
    """The gate runs on the resolved name, so this is what keeps a spelling
    from ever reaching a tool that writes."""
    assert set(_ALIASES.values()) <= set(READ_ONLY_TOOLS)


def test_a_directory_beyond_the_depth_is_named_not_dropped(context, worktree):
    """`src/` holds only `workbench/`, and listing files alone made it vanish:
    a model at depth 1 was never told the code existed."""
    (worktree / "src" / "pkg" / "deep").mkdir(parents=True)
    (worktree / "src" / "pkg" / "deep" / "mod.py").write_text("x = 1\n")

    listing = call(context, "list_files", path=".", depth=1).text.splitlines()

    assert "src/app.py" in listing
    assert "src/pkg/" in listing


def test_no_matches_says_what_a_pattern_matches(context):
    """A symbol and a filename in one regex returns nothing, forever, unless
    something says the pattern is matched against lines of text."""
    result = call(context, "search", pattern=r"main.*?app\.py")

    assert "not against file names" in result.text
    assert "path" in result.text


def test_query_is_understood_as_the_search_pattern(context):
    """gpt-oss's own spelling, sent four times in a row under pressure."""
    result = dispatch(RunPhase.PLAN, "search", {"query": "def main"}, context)

    assert isinstance(result, ToolResult)
    assert "src/app.py" in result.text


# --- Edits a model can actually make ------------------------------------------
#
# Every case here is a call gpt-oss really made in run 66, which read the right
# code and then spent fifteen turns failing to change it — and, when a change
# finally went through, inserted tabs into a space-indented function and wiped
# a 1,637-line test file.

LOOP = (
    "def approve(items):\n"
    "    if items:\n"
    "        for item in items:\n"
    "            make(item)\n"
    "        return 'made'\n"
    "    return 'none'\n"
)


@pytest.fixture
def loop_file(worktree) -> Path:
    path = worktree / "src" / "loop.py"
    path.write_text(LOOP, encoding="utf-8")
    return path


def test_a_pasted_gutter_is_removed_from_the_replacement(context, loop_file):
    result = call(
        context,
        "edit_file",
        path="src/loop.py",
        old_text="        return 'made'",
        new_text="5\t        finish()\n6\t        return 'made'",
    )

    assert not result.is_error, result.text
    assert "        finish()\n        return 'made'" in loop_file.read_text()


def test_a_replacement_still_carrying_line_numbers_is_refused(context, loop_file):
    """A number followed by spaces cannot be told apart from indentation, so
    it is refused with a reason rather than guessed at."""
    result = call(
        context,
        "edit_file",
        path="src/loop.py",
        old_text="        return 'made'",
        new_text="5        return 'done'",
    )

    assert result.is_error
    assert "line number" in result.text
    assert loop_file.read_text() == LOOP


def test_a_quote_with_a_retyped_gutter_still_finds_its_lines(context, loop_file):
    """Run 66's edit #3: the quote's gutter was retyped as spaces, a blank line
    became a bare number, and the replacement was correct under a tab gutter."""
    result = call(
        context,
        "edit_file",
        path="src/loop.py",
        old_text="3        for item in items:\n4            make(item)\n5        return 'made'",
        new_text=(
            "3\t        for item in items:\n4\t            make(item)\n"
            "5\t        finish()\n6\t        return 'made'"
        ),
    )

    assert not result.is_error, result.text
    assert (
        "            make(item)\n        finish()\n        return 'made'" in loop_file.read_text()
    )


def test_a_misindented_replacement_is_refused_not_moved(context, loop_file):
    """Run 66's edit #7, and the regression that shaped this design: a quote at
    one indentation and a replacement at another. Re-indenting to match put
    `return` inside the loop — valid Python, and wrong. It must be refused."""
    result = call(
        context,
        "edit_file",
        path="src/loop.py",
        old_text="            return 'made'",
        new_text="                finish()\n                return 'made'",
    )

    assert result.is_error
    assert "would no longer compile" in result.text
    assert loop_file.read_text() == LOOP


def test_tabs_into_a_file_indented_with_spaces_are_refused(context, loop_file):
    """Run 66's edit #8, which the old tool accepted."""
    result = call(
        context,
        "edit_file",
        path="src/loop.py",
        old_text="        return 'made'",
        new_text="\tfinish()\n\treturn 'made'",
    )

    assert result.is_error
    assert loop_file.read_text() == LOOP


def test_lines_can_be_replaced_by_number(context, loop_file):
    """How gpt-oss edits: it sent a line range in every edit of run 66."""
    result = call(
        context,
        "edit_file",
        path="src/loop.py",
        line_start=5,
        line_end=5,
        new_text="        finish()\n        return 'made'",
    )

    assert not result.is_error, result.text
    assert loop_file.read_text().count("\n") == LOOP.count("\n") + 1
    assert "5│        finish()" in result.text  # shows the model what it did


def test_a_line_past_the_end_appends(context, loop_file):
    result = call(
        context, "edit_file", path="src/loop.py", line_start=7, new_text="\n\ndef more():\n    pass"
    )

    assert not result.is_error, result.text
    assert loop_file.read_text().endswith("def more():\n    pass\n")


def test_a_range_outside_the_file_says_how_to_append(context, loop_file):
    result = call(context, "edit_file", path="src/loop.py", line_start=40, new_text="x = 1")

    assert result.is_error
    assert "line_start=7" in result.text


def test_a_file_already_broken_can_still_be_repaired(context, worktree):
    """Held to compiling only if it compiled before — or one bad edit would
    lock the model out of fixing it."""
    broken = worktree / "src" / "broken.py"
    broken.write_text("def f(:\n    return 1\n", encoding="utf-8")

    result = call(
        context, "edit_file", path="src/broken.py", old_text="def f(:", new_text="def f():"
    )

    assert not result.is_error, result.text


def test_write_file_will_not_shrink_a_long_file_to_a_fragment(context, worktree):
    """Run 66's edit #9: one new test written as the whole of a 1,637-line file."""
    long = worktree / "tests_long.py"
    long.write_text("".join(f"x_{i} = {i}\n" for i in range(100)), encoding="utf-8")

    result = call(
        context, "write_file", path="tests_long.py", content="def test_new():\n    pass\n"
    )

    assert result.is_error
    assert "line_start=101" in result.text
    assert long.read_text().count("\n") == 100


def test_write_file_still_creates_and_rewrites_small_files(context, worktree):
    assert not call(context, "write_file", path="new.py", content="x = 1\n").is_error
    assert not call(context, "write_file", path="new.py", content="y = 2\n").is_error
    assert (worktree / "new.py").read_text() == "y = 2\n"
