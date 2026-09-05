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
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
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
    offset = max(1, int(args.get("offset") or 1))
    limit = max(1, min(int(args.get("limit") or 400), 2000))
    window = lines[offset - 1 : offset - 1 + limit]
    # Numbered, because `edit_file` matches on text and a model that can cite
    # a line number is one that can quote the right text back.
    numbered = "\n".join(f"{offset + i}\t{line}" for i, line in enumerate(window))
    tail = ""
    if offset - 1 + limit < len(lines):
        remaining = len(lines) - (offset - 1 + limit)
        tail = f"\n… {remaining} more lines; read again with a later offset."
    return ToolResult(clip(numbered + tail) or "(empty file)")


def _search(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    pattern = str(args.get("pattern") or "").strip()
    if not pattern:
        return ToolResult("A pattern is required.", is_error=True)
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
        return ToolResult("No matches.")
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


def _write_file(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    target = _resolve(context, str(args.get("path") or ""))
    if isinstance(target, ToolResult):
        return target
    content = args.get("content")
    if not isinstance(content, str):
        return ToolResult("`content` must be a string.", is_error=True)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return ToolResult(f"Could not write {target}: {exc}", is_error=True)
    lines = len(content.splitlines())
    return ToolResult(f"Wrote {target.relative_to(context.worktree.resolve())} ({lines} lines).")


def _edit_file(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    target = _resolve(context, str(args.get("path") or ""))
    if isinstance(target, ToolResult):
        return target
    old = args.get("old_text")
    new = args.get("new_text")
    if not isinstance(old, str) or not isinstance(new, str):
        return ToolResult("`old_text` and `new_text` must both be strings.", is_error=True)
    if not target.is_file():
        return ToolResult(f"{target} is not a file.", is_error=True)

    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult(f"Could not read {target}: {exc}", is_error=True)

    count = content.count(old)
    if count == 0:
        return ToolResult(
            "That text is not in the file. Read it again and quote it exactly, "
            "including indentation.",
            is_error=True,
        )
    if count > 1 and not bool(args.get("replace_all")):
        return ToolResult(
            f"That text appears {count} times. Include more surrounding lines to "
            "make it unique, or pass replace_all.",
            is_error=True,
        )

    try:
        target.write_text(content.replace(old, new), encoding="utf-8")
    except OSError as exc:
        return ToolResult(f"Could not write {target}: {exc}", is_error=True)
    where = target.relative_to(context.worktree.resolve())
    return ToolResult(f"Edited {where} ({count} replacement{'s' if count > 1 else ''}).")


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
    outcome = str(args.get("outcome") or "").strip()
    if outcome not in {"finished", "failed", "needs_replanning"}:
        return ToolResult(
            "`outcome` must be one of finished, failed, needs_replanning.", is_error=True
        )
    payload = {"outcome": outcome, "detail": str(args.get("detail") or "") or None}

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
    return ToolResult(f"Recorded outcome: {outcome}.")


def _submit_plan(context: ToolContext, args: dict[str, Any]) -> ToolOutcome:
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
            description="List files under a directory in the worktree.",
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
            description="Search the worktree for a regular expression, like grep -rn.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "A grep regular expression."},
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
            description="Create a file, or replace one entirely. Prefer edit_file for changes.",
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
                "Replace an exact piece of text in a file. The old text must appear "
                "exactly once unless replace_all is set."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": _PATH_PROPERTY,
                    "old_text": {"type": "string", "description": "Text to replace, verbatim."},
                    "new_text": {"type": "string", "description": "What to put in its place."},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence."},
                },
                "required": ["path", "old_text", "new_text"],
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
)


def tool_names_for(phase: RunPhase) -> tuple[str, ...]:
    return READ_ONLY_TOOLS if phase is RunPhase.PLAN else WORKING_TOOLS


def tools_for(phase: RunPhase) -> list[dict[str, Any]]:
    """The tool schemas to send with a request, for one phase."""
    return [TOOLS[name].schema() for name in tool_names_for(phase)]


def dispatch(phase: RunPhase, name: str, args: dict[str, Any], context: ToolContext) -> ToolOutcome:
    """Run one tool call, or explain why it did not happen.

    Refuses a tool that exists but is not offered in this phase, rather than
    running it: a model that hallucinates `run_command` during planning must
    not get a shell because the dispatcher was more permissive than the
    schema it was handed.
    """
    if name not in tool_names_for(phase):
        available = ", ".join(tool_names_for(phase))
        known = " It is not available in this phase." if name in TOOLS else ""
        return ToolResult(
            f"There is no tool called {name!r} here.{known} Available: {available}.",
            is_error=True,
        )
    try:
        return TOOLS[name].handler(context, args)
    except Exception as exc:
        # A crashing tool is the model's problem to route around, not the
        # run's to die of. The traceback still reaches the journal.
        logger.exception("Tool %s failed.", name)
        return ToolResult(f"The {name} tool failed: {exc}", is_error=True)
