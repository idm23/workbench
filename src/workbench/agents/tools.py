"""The tools a locally-served model is given, and what they do.

A hosted coding agent arrives with its own tools; a model behind a plain
`/chat/completions` endpoint arrives with none, so Workbench supplies them.
That is the real cost of the local backend and also its one advantage: what
the agent can do is a list in this file rather than a vendor's decision.

Two properties are worth stating because they are load-bearing rather than
incidental.

**Every path is confined to the worktree.** Not as a security boundary — the
containment is the unprivileged service account, exactly as it is for the
Claude backend, and `run_command` can obviously reach past it — but because a
model that wanders into `/srv` and edits the deployment is the failure mode of
a small model, and refusing it by construction is cheaper than noticing later.

**The plan phase is enforced by absence.** The Claude backend gets read-only
planning from the SDK's plan mode; here it comes from `tools_for` simply not
returning the tools that write. There is nothing to bypass, which makes this
the stronger of the two guarantees rather than the improvised one.
"""

import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from workbench.agents.protocol import SubtaskProposal
from workbench.config import agent_environment
from workbench.database.models import RunPhase

logger = logging.getLogger(__name__)

#: Longest tool output handed back. The cap earns its keep twice: every result
#: is an event row kept forever, and every result is also spent out of a
#: context window that on this hardware is measured in tens of thousands of
#: tokens rather than hundreds. The window is the tighter of the two, which is
#: why this is smaller than the equivalent in the Claude adapter.
MAX_OUTPUT_CHARS = 6_000

#: Directories never worth walking into or searching. Not a correctness
#: measure — a model can still `read_file` into any of them — but the
#: difference between a listing a small model can use and one that buries the
#: source tree under a virtualenv.
SKIPPED_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache"})

#: How long `search` may take before it is the thing that is stuck.
SEARCH_TIMEOUT_SECONDS = 30

#: The default and ceiling for `run_command`. A command that has said nothing
#: for five minutes is not going to, and the run's own systemd timeout is an
#: hour — a shell command must not be allowed to spend all of it.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120
MAX_COMMAND_TIMEOUT_SECONDS = 600

#: How long the outcome report may take. Local, unauthenticated, and on the
#: happy path a few milliseconds.
OUTCOME_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool needs that is not one of its own arguments."""

    worktree: Path
    #: Workbench's own API, so `report_outcome` reaches the same endpoint the
    #: Claude backend's skill curls. One endpoint, one code path above it.
    api_base: str
    run_id: int = 0
    task_id: int = 0
    project_id: int = 0

    #: Which tools have been called in this run so far. Mutable inside a frozen
    #: container on purpose: it is the run's history rather than its
    #: configuration, and two tools below refuse to be the *first* thing a run
    #: does — see `_nothing_looked_at`.
    used: set[str] = field(default_factory=set)

    #: The worktree's HEAD when the run started, so `report_outcome` can tell
    #: whether anything actually happened. None when the backend could not read
    #: it, which reads as "cannot say" rather than as "nothing changed".
    head_at_start: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """What a tool hands back to the model, and to the event log."""

    text: str
    is_error: bool = False


@dataclass(frozen=True)
class PlanSubmitted:
    """The plan phase's product, which ends the run rather than continuing it.

    A result type rather than a `ToolResult` with a magic string, so the loop
    branches on a type and cannot mistake a plan for an ordinary tool call.
    """

    plan: str
    subtasks: list[SubtaskProposal]


type ToolOutcome = ToolResult | PlanSubmitted


@dataclass(frozen=True)
class Tool:
    """One callable, in the shape `/chat/completions` expects to be told about."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[ToolContext, dict[str, Any]], ToolOutcome]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def clip(text: str) -> str:
    """Bound a string, saying so when it is cut.

    Saying so matters more here than in a log: the model reads this, and text
    that stops mid-sentence with no marker is text it will treat as the whole
    answer.
    """
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    dropped = len(text) - MAX_OUTPUT_CHARS
    return f"{text[:MAX_OUTPUT_CHARS]}\n… truncated, {dropped} more characters"


def _resolve(context: ToolContext, raw: str) -> Path | ToolResult:
    """A path inside the worktree, or the refusal to use one outside it.

    `resolve()` before comparing, so `..` and a symlink pointing out of the
    tree are the same case and both are caught.
    """
    root = context.worktree.resolve()
    candidate = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    if candidate != root and root not in candidate.parents:
        return ToolResult(
            f"Refused: {raw} is outside the worktree. Every path must be inside {root}.",
            is_error=True,
        )
    return candidate


def _list_files(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    target = _resolve(context, str(args.get("path") or "."))
    if isinstance(target, ToolResult):
        return target
    if not target.is_dir():
        return ToolResult(f"{target} is not a directory.", is_error=True)

    root = context.worktree.resolve()
    depth = max(1, min(int(args.get("depth") or 2), 6))
    lines: list[str] = []
    for current, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
        here = Path(current)
        if len(here.relative_to(target).parts) >= depth:
            # Named, not silently dropped. Listing only files meant a directory
            # holding nothing but directories vanished outright: `src/` holds
            # only `workbench/`, so at depth 1 a model was never told `src`
            # existed, and qwen3 spent a planning run guessing `app` and `runs`
            # at the top level. A trailing slash says "there is more in here".
            lines.extend(f"{(here / name).relative_to(root)}/" for name in dirnames)
            dirnames.clear()
        # Filtered out of the files too, not only the directories: inside a
        # worktree `.git` is a *file* holding a pointer to the real gitdir, so
        # a listing that only skipped directories showed it at the top of
        # every listing the agent ever asked for.
        for name in sorted(n for n in filenames if n not in SKIPPED_DIRS):
            lines.append(str((here / name).relative_to(root)))
        if len(lines) > 500:
            lines.append("… more files not listed; narrow the path or the depth.")
            break
    return ToolResult(clip("\n".join(lines) or "(no files)"))


def _read_file(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    target = _resolve(context, str(args.get("path") or ""))
    if isinstance(target, ToolResult):
        return target
    if not target.is_file():
        return ToolResult(f"{target} is not a file.", is_error=True)

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ToolResult(f"Could not read {target}: {exc}", is_error=True)

    lines = content.splitlines()
    offset = max(1, int(args.get("offset") or args.get("line_start") or 1))
    requested = args.get("limit")
    if requested is None and args.get("line_end") is not None:
        # gpt-oss's own spelling is an inclusive range — see `_ALIASES`.
        requested = int(args["line_end"]) - offset + 1
    limit = max(1, min(int(requested or 400), 2000))
    window = lines[offset - 1 : offset - 1 + limit]
    # Numbered, because a model that can cite a line number can edit by it. The
    # gutter is `│`, not a tab: with a tab, gpt-oss read the separator as part
    # of the indentation and wrote tab-indented code into a file indented with
    # spaces. Whatever it copies back is stripped again — see `_ungutter`.
    numbered = "\n".join(f"{offset + i}{GUTTER}{line}" for i, line in enumerate(window))
    tail = ""
    if offset - 1 + limit < len(lines):
        remaining = len(lines) - (offset - 1 + limit)
        tail = f"\n… {remaining} more lines; read again with a later offset."
    return ToolResult(clip(numbered + tail) or "(empty file)")


def _search(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    # `query` is gpt-oss's spelling, from the search tool it was trained with;
    # under a tight window it reached for it four times running and was told
    # only that a pattern was required. Same bargain as `_ALIASES`.
    pattern = str(args.get("pattern") or args.get("query") or "").strip()
    if not pattern:
        return ToolResult(
            "search needs `pattern`: the text or regular expression to find.", is_error=True
        )
    target = _resolve(context, str(args.get("path") or "."))
    if isinstance(target, ToolResult):
        return target

    argv = ["grep", "-rnI", "--color=never"]
    argv += [f"--exclude-dir={name}" for name in sorted(SKIPPED_DIRS)]
    argv += ["-e", pattern, str(target)]
    try:
        # No shell: the pattern comes from a model, and `-e` keeps one that
        # starts with a dash from being read as an option.
        found = subprocess.run(
            argv, capture_output=True, text=True, timeout=SEARCH_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError:
        return ToolResult("grep is not installed on this machine.", is_error=True)
    except subprocess.TimeoutExpired:
        return ToolResult(f"Search timed out after {SEARCH_TIMEOUT_SECONDS}s.", is_error=True)

    if found.returncode == 1:
        # The hint is for one specific mistake, made eleven times in one run:
        # `approve_plan.*?app\.py`, a symbol and a filename in one regex, as if
        # the pattern matched paths. It matches lines of text.
        return ToolResult(
            f"No matches for {pattern!r}. The pattern is matched against each line "
            "of file contents, not against file names — to look inside one file or "
            "directory, pass it as `path` and search for the text alone."
        )
    if found.returncode > 1:
        return ToolResult(clip(found.stderr or "Search failed."), is_error=True)
    root = context.worktree.resolve()
    relative = found.stdout.replace(f"{root}/", "")
    return ToolResult(clip(relative))


def _run_command(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    command = str(args.get("command") or "").strip()
    if not command:
        return ToolResult("A command is required.", is_error=True)
    timeout = max(
        1,
        min(
            int(args.get("timeout") or DEFAULT_COMMAND_TIMEOUT_SECONDS), MAX_COMMAND_TIMEOUT_SECONDS
        ),
    )

    try:
        completed = subprocess.run(
            ["bash", "-lc", command],
            cwd=context.worktree,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=agent_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(f"Command timed out after {timeout}s.", is_error=True)
    except OSError as exc:
        return ToolResult(f"Command could not be started: {exc}", is_error=True)

    body = completed.stdout
    if completed.stderr:
        body = f"{body}\n[stderr]\n{completed.stderr}" if body else completed.stderr
    report = f"[exit {completed.returncode}]\n{body}".rstrip()
    # A non-zero exit is information, not a tool failure: the model asked what
    # happens and this is what happened. Marking it an error invites a retry
    # loop over a test that is legitimately red.
    return ToolResult(clip(report))


#: What separates a line number from the line in `read_file`'s output.
GUTTER = "│"

#: Line-number gutters a model copies back from `read_file`. Two strengths,
#: because the two halves of an edit are different kinds of text.
#:
#: Quoted text only has to *find* something, so any retyping is accepted —
#: including a bare number followed by spaces, which is what a tab gutter
#: became when gpt-oss typed it back. Replacement text is *content*, so only a
#: gutter that cannot be mistaken for indentation is removed: this tool's `│`,
#: or a tab. A bare number alone is how a blank line looks in either.
_LOOSE_GUTTER = re.compile(r"^\s*\d+(?:│|\t|: |\| | +|$)")
_EXACT_GUTTER = re.compile(r"^\s*\d+(?:│|\t|$)")
_STILL_NUMBERED = re.compile(r"^\s*\d+\s")

#: Below this many lines a whole-file rewrite is ordinary, not a warning sign.
_REWRITE_GRACE_LINES = 20


def _ungutter(text: str, pattern: re.Pattern[str]) -> str:
    """Remove a pasted line-number gutter — only when *every* line carries one.

    The all-lines rule is what keeps real code that starts with a number from
    being mangled. gpt-oss pasted `738\t    if proposed:` back five times in one
    run; each of those edits failed on the gutter, not on the code.
    """
    lines = text.split("\n")
    body = [line for line in lines if line.strip()]
    if not body or not all(pattern.match(line) for line in body):
        return text
    return "\n".join(pattern.sub("", line, count=1) for line in lines)


def _locate(content: str, old: str, near: int | None) -> tuple[int, int] | str:
    """Find quoted lines ignoring indentation: (first, end) line indexes, or why not.

    Locating only — the replacement is then used exactly as sent. An earlier
    version re-indented it to match, and turned an inconsistently indented
    quote into `return` *inside* a loop: code that compiled, and would have
    created one subtask and stopped. A refusal is better than that.
    """
    wanted = [line.strip() for line in old.strip("\n").split("\n")]
    lines = content.split("\n")
    stripped = [line.strip() for line in lines]
    hits = [
        i for i in range(len(lines) - len(wanted) + 1) if stripped[i : i + len(wanted)] == wanted
    ]
    if not hits:
        return (
            "That text is not in the file. Read it again and quote it exactly, "
            "or pass line_start and line_end instead."
        )
    if len(hits) > 1:
        if near is None:
            places = ", ".join(str(i + 1) for i in hits[:5])
            return f"That text appears at lines {places}. Pass line_start to say which."
        hits.sort(key=lambda i: abs(i - near))
    return hits[0], hits[0] + len(wanted)


def _broken_python(target: Path, before: str | None, after: str) -> str | None:
    """A syntax error this change would introduce into a Python file, if any.

    Refused before it is written, because the alternative is what happened: an
    edit that "succeeded" by inserting tab-indented lines into a space-indented
    function, left for tests the run never reached to find. A file that did not
    compile *before* is not held to it, or one bad edit would lock the model out
    of repairing its own mistake.
    """
    if target.suffix != ".py":
        return None
    if before is not None:
        try:
            compile(before, str(target), "exec")
        except SyntaxError:
            return None
    try:
        compile(after, str(target), "exec")
    except SyntaxError as exc:
        return f"line {exc.lineno}: {exc.msg}"
    return None


def _write_file(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    target = _resolve(context, str(args.get("path") or ""))
    if isinstance(target, ToolResult):
        return target
    content = args.get("content")
    if not isinstance(content, str):
        return ToolResult("`content` must be a string.", is_error=True)

    before: str | None = None
    if target.is_file():
        try:
            before = target.read_text(encoding="utf-8")
        except OSError:
            before = None
    if before is not None:
        had, would = len(before.splitlines()), len(content.splitlines())
        # gpt-oss meant to add one test to a 1,637-line file and wrote the test
        # as the whole file. Nobody wants that rewrite.
        if had > _REWRITE_GRACE_LINES and would < had // 2:
            return ToolResult(
                f"Refused: {target.relative_to(context.worktree.resolve())} has {had} lines "
                f"and this would replace all of them with {would}. write_file replaces the "
                f"whole file. To add to it, use edit_file with line_start={had + 1} and your "
                "new lines as new_text; to change part of it, use edit_file.",
                is_error=True,
            )
    if (broken := _broken_python(target, before, content)) is not None:
        return ToolResult(f"Not written — it would not compile: {broken}.", is_error=True)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolResult(f"Could not write {target}: {exc}", is_error=True)
    lines = len(content.splitlines())
    return ToolResult(f"Wrote {target.relative_to(context.worktree.resolve())} ({lines} lines).")


def _around(content: str, first: int, last: int) -> str:
    """The edited lines with a little context, numbered, so the model can check."""
    lines = content.split("\n")
    lo, hi = max(0, first - 2), min(len(lines), last + 2)
    return "\n".join(f"{i + 1}{GUTTER}{lines[i]}" for i in range(lo, hi))


def _replace_lines(content: str, first: int, end: int, new: str) -> str:
    """Lines [first, end) replaced by `new`, keeping the file's final newline."""
    trailing = content.endswith("\n")
    lines = (content[:-1] if trailing else content).split("\n")
    replacement = (new[:-1] if new.endswith("\n") else new).split("\n")
    lines[first : min(end, len(lines))] = replacement
    return "\n".join(lines) + ("\n" if trailing else "")


def _edit_file(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """Change part of a file, by quoting it or by naming its lines.

    Two ways in, because two models edit two ways. Quoting (`old_text`) stays
    the default. Naming lines (`line_start`, `line_end`) is how gpt-oss edits —
    it sent a line range in all eight edits of one run, and refusing it cost
    that run most of its turns.
    """
    target = _resolve(context, str(args.get("path") or ""))
    if isinstance(target, ToolResult):
        return target
    new = args.get("new_text", args.get("content"))
    if not isinstance(new, str):
        return ToolResult("`new_text` must be a string.", is_error=True)
    if not target.is_file():
        return ToolResult(f"{target} is not a file.", is_error=True)
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult(f"Could not read {target}: {exc}", is_error=True)

    new = _ungutter(new, _EXACT_GUTTER)
    body = [line for line in new.split("\n") if line.strip()]
    if body and all(_STILL_NUMBERED.match(line) for line in body):
        return ToolResult(
            "new_text starts every line with a line number. Send only the code, "
            "without the numbers read_file shows.",
            is_error=True,
        )

    try:
        near = int(args["line_start"]) - 1 if args.get("line_start") is not None else None
        end_line = int(args.get("line_end") or args["line_start"]) if near is not None else 0
    except TypeError, ValueError:
        return ToolResult("line_start and line_end must be line numbers.", is_error=True)

    old = args.get("old_text")
    if isinstance(old, str) and old.strip():
        count = content.count(old)
        if count > 1 and not bool(args.get("replace_all")):
            return ToolResult(
                f"That text appears {count} times. Include more surrounding lines to "
                "make it unique, or pass replace_all.",
                is_error=True,
            )
        if count:
            index = content.index(old)
            updated = content.replace(old, new) if count > 1 else content.replace(old, new, 1)
            first = content.count("\n", 0, index)
        else:
            found = _locate(content, _ungutter(old, _LOOSE_GUTTER), near)
            if isinstance(found, str):
                return ToolResult(found, is_error=True)
            first, end = found
            updated = _replace_lines(content, first, end, new)
    elif near is not None:
        total = len(content.splitlines())
        if near < 0 or near > total or end_line < near:
            return ToolResult(
                f"Lines {near + 1} to {end_line} are outside the file, which has {total} "
                f"lines. Use line_start={total + 1} to add to the end.",
                is_error=True,
            )
        first = near
        updated = _replace_lines(content, near, end_line, new)
    else:
        return ToolResult(
            "Say what to change: old_text to quote it, or line_start and line_end to "
            "name its lines.",
            is_error=True,
        )

    if (broken := _broken_python(target, content, updated)) is not None:
        return ToolResult(
            f"Not applied — the file would no longer compile: {broken}. Check the "
            "indentation matches the lines around it.",
            is_error=True,
        )
    try:
        target.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return ToolResult(f"Could not write {target}: {exc}", is_error=True)
    where = target.relative_to(context.worktree.resolve())
    last = first + new.count("\n") + 1
    return ToolResult(f"Edited {where}.\n{_around(updated, first, last)}")


def _git(context: ToolContext, *args: str) -> str | None:
    """One read-only git command in the worktree, or None if it could not run."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=context.worktree,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def nothing_happened(context: ToolContext) -> bool:
    """Whether this run has left the worktree exactly as it found it.

    Both halves matter. A commit moves HEAD; work in progress shows in
    `status`. Only when neither has changed is "finished" a claim about
    nothing, and only then is it worth refusing.

    Answers False whenever it cannot tell. A tool that blocks an outcome on a
    git command that did not run would turn a broken probe into a run that can
    never report anything.
    """
    if context.head_at_start is None:
        return False
    head = _git(context, "rev-parse", "HEAD")
    if head is None or head != context.head_at_start:
        return False
    return _git(context, "status", "--porcelain") == ""


def uncommitted_work(context: ToolContext) -> bool:
    """Whether the worktree holds changes nobody has committed yet.

    False when it cannot tell, for the same reason as `nothing_happened`: a
    probe that failed must not be what keeps a run from ending.
    """
    status = _git(context, "status", "--porcelain")
    return bool(status)


def _nothing_looked_at(context: ToolContext, tool: str) -> ToolResult | None:
    """Refuse a verdict from a run that has not looked at anything yet.

    The first real run against a 7B ended on turn one: it reported
    `needs_replanning` — "the specification is too vague" — having read no
    file, listed no directory, and run no command. That is not a judgement, it
    is a reflex, and the run recorded it as an outcome.

    So the two tools that end a run refuse to be the first thing it does. The
    message says what to do instead, which is what a small model needs; a task
    that genuinely cannot be done can still be reported after one look.
    """
    if context.used - {tool}:
        return None
    return ToolResult(
        "Not yet — this run has not looked at anything. Read the files the task "
        "mentions first, then decide. If it still cannot be done, call this again "
        "and say what you found.",
        is_error=True,
    )


def _ask_user(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """Ask the person a question, and end the run waiting for the answer.

    Reported through the same endpoint as any other outcome, because that is
    what it is: the agent saying how the run went. Recorded live, so the
    question survives the process that asked it.

    The refusal below is the same one `report_outcome` gets, and matters more
    here: a question asked before reading anything is not a question, it is a
    reflex, and every one of them costs a person their attention.
    """
    if (refusal := _nothing_looked_at(context, "ask_user")) is not None:
        return refusal

    question = str(args.get("question") or "").strip()
    if not question:
        return ToolResult("`question` must not be empty.", is_error=True)

    result = _report(context, {"outcome": "needs_answer", "detail": question})
    if result.is_error:
        return result
    return ToolResult(
        "Asked. Stop now: say briefly what you were doing and what you need to "
        "know, and make no further tool calls. The run ends here and resumes "
        "with the answer."
    )


def _report_outcome(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    """Tell Workbench how the task went, through its own API.

    The same endpoint the Claude backend's skill curls, called the same way,
    so the two backends converge on one code path above the seam rather than
    on two ways of recording the same decision.

    A failure to reach it is reported to the model and nothing more. The run
    still has its work and its summary, and an unreported run is already
    defined as "not assumed to have succeeded" — turning a lost HTTP call into
    a failed run would throw away the commits with it.
    """
    if (refusal := _nothing_looked_at(context, "report_outcome")) is not None:
        return refusal

    outcome = str(args.get("outcome") or "").strip()
    if outcome not in {"finished", "failed", "needs_replanning"}:
        return ToolResult(
            "`outcome` must be one of finished, failed, needs_replanning.", is_error=True
        )

    if outcome == "finished" and nothing_happened(context):
        # Observed on the second real run: a batch of tool calls composed
        # before any of them had run, an edit that failed because the file had
        # not been read yet, a commit with nothing staged — and then
        # `finished`, which marked the task done. Workbench noticed there were
        # no commits and declined to push; nothing declined the claim itself.
        return ToolResult(
            "The worktree is exactly as you found it: nothing committed, nothing "
            "changed. Do the work first, or report failed or needs_replanning and "
            "say what stopped you.",
            is_error=True,
        )
    return _report(context, {"outcome": outcome, "detail": str(args.get("detail") or "") or None})


def _report(context: ToolContext, payload: dict[str, Any]) -> ToolResult:
    """POST one outcome to Workbench's own API. Shared by both tools that
    report one, so there is a single place that knows the route and a single
    behaviour when it cannot be reached."""
    try:
        response = httpx.post(
            f"{context.api_base}/api/runs/{context.run_id}/outcome",
            json=payload,
            timeout=OUTCOME_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Could not report outcome for run %s: %s", context.run_id, exc)
        return ToolResult(
            f"Workbench did not accept the outcome ({exc}). Carry on and say so in "
            "your summary; do not retry more than once.",
            is_error=True,
        )
    return ToolResult(f"Recorded outcome: {payload['outcome']}.")


def _submit_plan(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    if (refusal := _nothing_looked_at(context, "submit_plan")) is not None:
        return refusal

    plan = str(args.get("plan") or "").strip()
    if not plan:
        return ToolResult("`plan` must not be empty.", is_error=True)

    raw = args.get("subtasks")
    proposals: list[SubtaskProposal] = []
    # Defensive despite the schema, for the same reason the Claude adapter is:
    # a model can deviate from what it was told to send, and a small one will.
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        proposals.append(
            SubtaskProposal(
                title=str(item["title"]).strip(),
                body=str(item.get("body") or "").strip(),
                ready_to_execute=bool(item.get("ready_to_execute", False)),
            )
        )
    return PlanSubmitted(plan=plan, subtasks=proposals)


_PATH_PROPERTY = {"type": "string", "description": "Path relative to the worktree root."}


#: Every tool, by name. Availability by phase is `tools_for` below; this is
#: only the definition, so a tool cannot exist in one phase's list and be
#: unimplemented in the dispatcher.
TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            name="list_files",
            description=(
                "List files under a directory in the worktree. Directories beyond "
                "the depth are shown with a trailing slash; list them to see inside."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": _PATH_PROPERTY,
                    "depth": {"type": "integer", "description": "How many levels deep, 1-6."},
                },
            },
            handler=_list_files,
        ),
        Tool(
            name="read_file",
            description="Read a file from the worktree, with line numbers.",
            parameters={
                "type": "object",
                "properties": {
                    "path": _PATH_PROPERTY,
                    "offset": {"type": "integer", "description": "First line to read, 1-based."},
                    "limit": {"type": "integer", "description": "How many lines to read."},
                },
                "required": ["path"],
            },
            handler=_read_file,
        ),
        Tool(
            name="search",
            description=(
                "Search file contents for a regular expression, like grep -rn. The "
                "pattern matches lines of text, never file names; use `path` to "
                "search inside one file or directory."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text or a grep regular expression to find in lines.",
                    },
                    "path": _PATH_PROPERTY,
                },
                "required": ["pattern"],
            },
            handler=_search,
        ),
        Tool(
            name="run_command",
            description=(
                "Run a shell command in the worktree. Use this for git, tests, and "
                "build tools. Commits happen here; never push."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command."},
                    "timeout": {"type": "integer", "description": "Seconds to allow, up to 600."},
                },
                "required": ["command"],
            },
            handler=_run_command,
        ),
        Tool(
            name="write_file",
            description=(
                "Create a new file, or replace one entirely. To add to or change an "
                "existing file, use edit_file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": _PATH_PROPERTY,
                    "content": {"type": "string", "description": "The complete file contents."},
                },
                "required": ["path", "content"],
            },
            handler=_write_file,
        ),
        Tool(
            name="edit_file",
            description=(
                "Change part of a file. Either quote the text to replace in old_text "
                "(it must appear once, unless replace_all), or give line_start and "
                "line_end to replace those lines. line_start one past the last line "
                "adds to the end. Line numbers are the ones read_file shows."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": _PATH_PROPERTY,
                    "old_text": {"type": "string", "description": "Text to replace, verbatim."},
                    "line_start": {"type": "integer", "description": "First line to replace."},
                    "line_end": {"type": "integer", "description": "Last line to replace."},
                    "new_text": {"type": "string", "description": "What to put in its place."},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence."},
                },
                "required": ["path", "new_text"],
            },
            handler=_edit_file,
        ),
        Tool(
            name="report_outcome",
            description=(
                "Tell Workbench how this task went. Call it once, near the end, "
                "before your final summary."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "outcome": {
                        "type": "string",
                        "enum": ["finished", "failed", "needs_replanning"],
                        "description": (
                            "finished only when the work is complete and committed; "
                            "failed when you hit something you could not work around; "
                            "needs_replanning when the task itself turned out wrong."
                        ),
                    },
                    "detail": {"type": "string", "description": "One line of context."},
                },
                "required": ["outcome"],
            },
            handler=_report_outcome,
        ),
        Tool(
            name="ask_user",
            description=(
                "Ask the person a question and stop. Use this only for a fork you "
                "genuinely cannot settle and that changes what gets built — not for "
                "a detail you could choose and mention in your summary. The run ends "
                "here and resumes with their answer, so ask everything you need in "
                "one question."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "What you need to know, and enough context to answer it "
                            "without reading the transcript."
                        ),
                    },
                },
                "required": ["question"],
            },
            handler=_ask_user,
        ),
        Tool(
            name="submit_plan",
            description=(
                "Deliver the plan and end the planning run. Call this exactly once, "
                "when your investigation is done."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan": {"type": "string", "description": "The plan, in prose."},
                    "subtasks": {
                        "type": "array",
                        "description": "Only if the task genuinely needs splitting up.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "body": {"type": "string"},
                                "ready_to_execute": {"type": "boolean"},
                            },
                            "required": ["title", "body", "ready_to_execute"],
                        },
                    },
                },
                "required": ["plan"],
            },
            handler=_submit_plan,
        ),
    )
}

#: The tools that end a run by reporting how it went. A run that has called
#: one of these has finished on purpose, which is what makes stopping right
#: afterwards correct rather than a stall — see the nudge in `agents/local.py`,
#: which fired twice at a run that had just asked a question and was waiting
#: for the answer.
OUTCOME_TOOLS = frozenset({"report_outcome", "ask_user"})

#: What the plan phase gets: enough to investigate, nothing that writes and no
#: shell. See the module docstring — this tuple *is* the read-only guarantee.
READ_ONLY_TOOLS = ("list_files", "read_file", "search", "submit_plan")

#: What the other phases get. `submit_plan` is deliberately absent: a run that
#: is carrying work out has no plan to deliver, and offering it invites a
#: model to end the run by describing what it was about to do.
WORKING_TOOLS = (
    "list_files",
    "read_file",
    "search",
    "run_command",
    "write_file",
    "edit_file",
    "report_outcome",
    "ask_user",
)


def tool_names_for(phase: RunPhase) -> tuple[str, ...]:
    return READ_ONLY_TOOLS if phase is RunPhase.PLAN else WORKING_TOOLS


def tools_for(phase: RunPhase) -> list[dict[str, Any]]:
    """The tool schemas to send with a request, for one phase."""
    return [TOOLS[name].schema() for name in tool_names_for(phase)]


#: Names a model reaches for that mean a tool this loop already has.
#:
#: gpt-oss asks for `open_file(path, line_start, line_end)` — its own trained
#: spelling of reading a file — and asked for it 14 times across two planning
#: runs on one task, plus `view_file` once, against a list that offers
#: `read_file`. Every one was refused with the list of real names, and it kept
#: asking: up to eight of forty turns spent on a tool that did not exist.
#: Understanding the spelling is the same bargain `_apply_delta` makes with
#: three names for reasoning, and cheaper than teaching one model another.
#:
#: Aliases point only at read-only tools, and that is checked structurally
#: (`test_every_alias_is_read_only`): the phase gate runs on the *resolved*
#: name, so no spelling can reach a tool the phase was not offered.
_ALIASES = {"open_file": "read_file", "view_file": "read_file"}


def dispatch(phase: RunPhase, name: str, args: dict[str, Any], context: ToolContext) -> ToolOutcome:
    """Run one tool call, or explain why it did not happen.

    Refuses a tool that exists but is not offered in this phase, rather than
    running it: a model that hallucinates `run_command` during planning must
    not get a shell because the dispatcher was more permissive than the
    schema it was handed.
    """
    name = _ALIASES.get(name, name)
    if name not in tool_names_for(phase):
        available = ", ".join(tool_names_for(phase))
        known = " It is not available in this phase." if name in TOOLS else ""
        return ToolResult(
            f"There is no tool called {name!r} here.{known} Available: {available}.",
            is_error=True,
        )
    context.used.add(name)
    try:
        return TOOLS[name].handler(context, args)
    except Exception as exc:
        # A crashing tool is the model's problem to route around, not the
        # run's to die of. The traceback still reaches the journal.
        logger.exception("Tool %s failed.", name)
        return ToolResult(f"The {name} tool failed: {exc}", is_error=True)
