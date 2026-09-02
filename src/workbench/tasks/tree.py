"""Assembling a flat list of tasks into the tree the page renders.

Kept separate from the routes and free of database access so the shape of the
tree — ordering, nesting, progress counts — can be tested directly. The web
layer loads rows; this decides what they look like.
"""

from dataclasses import dataclass, field

from workbench.database.models import RunPhase, Task, TaskStatus
from workbench.runs.activity import TaskActivity

#: Guards against a parent cycle rendering forever. Nothing legitimate nests
#: anywhere near this deep; hitting it means the data is corrupt.
MAX_DEPTH = 20

#: How much of a task's body the tree shows before truncating. Long enough
#: for a sentence or two of context, short enough that a planning note
#: written for an agent — which can run to several paragraphs — does not
#: dominate a page meant to be scanned from a phone.
BODY_PREVIEW_CHARS = 220


def _truncate_at_word(text: str, limit: int) -> str:
    """`text` cut to at most `limit` characters, backing up to the last
    space so a preview never ends mid-word."""
    cut = text[:limit]
    last_space = cut.rfind(" ")
    if last_space > 0:
        cut = cut[:last_space]
    return cut


@dataclass
class TaskNode:
    """A task plus its place in the tree."""

    task: Task
    depth: int
    children: list[TaskNode] = field(default_factory=list)

    @property
    def is_leaf(self) -> bool:
        """Only leaves can be run.

        A task with children describes work rather than being work — the
        children are the work — so pointing an agent at one would give it no
        single thing to do.
        """
        return not self.children

    @property
    def done_count(self) -> int:
        return sum(1 for child in self.children if child.effective_status is TaskStatus.DONE)

    @property
    def progress(self) -> str | None:
        """Progress like `2/5` for a parent, or None for a leaf."""
        if not self.children:
            return None
        return f"{self.done_count}/{len(self.children)}"

    @property
    def progress_percent(self) -> int | None:
        """`progress` as a 0-100 int, for a meter bar rather than only text."""
        if not self.children:
            return None
        return round(100 * self.done_count / len(self.children))

    @property
    def body_preview(self) -> str | None:
        """The task's body, cut to a skimmable length — or all of it, when
        it already fits. `None` when there is no body at all."""
        body = self.task.body
        if not body:
            return None
        if len(body) <= BODY_PREVIEW_CHARS:
            return body
        return _truncate_at_word(body, BODY_PREVIEW_CHARS)

    @property
    def body_rest(self) -> str | None:
        """Whatever `body_preview` left out, for a "show more" toggle to
        reveal — `None` when nothing was cut, which is also the page's cue
        that there is nothing to expand."""
        body = self.task.body
        preview = self.body_preview
        if not body or preview is None or len(body) <= BODY_PREVIEW_CHARS:
            return None
        return body[len(preview) :].lstrip()

    @property
    def effective_status(self) -> TaskStatus:
        """The status to render, deriving from children once there are any.

        A task that decomposed into subtasks is itself "in progress" the
        moment it has them, not only once a child gets picked up — this is
        what answers CLAUDE.md's open question of whether a parent's status
        should derive from its children: yes. A manually-set terminal status
        (done or cancelled) is a person's own word and is respected regardless
        of what the children say — nothing here writes to `task.status`, this
        only changes what a page renders.
        """
        if not self.children:
            return self.task.status
        if self.task.status in (TaskStatus.DONE, TaskStatus.CANCELLED):
            return self.task.status

        statuses = [child.effective_status for child in self.children]
        if any(status is TaskStatus.BLOCKED for status in statuses):
            return TaskStatus.BLOCKED
        if all(status is TaskStatus.DONE for status in statuses):
            return TaskStatus.DONE
        return TaskStatus.ACTIVE


def build_tree(tasks: list[Task]) -> list[TaskNode]:
    """Turn a flat list into roots with nested children.

    Tasks whose parent is missing from the input are treated as roots rather
    than dropped: showing an orphan in the wrong place beats it vanishing from
    the page with no indication anything is missing.
    """
    nodes = {task.id: TaskNode(task=task, depth=0) for task in tasks}
    roots: list[TaskNode] = []

    for task in tasks:
        node = nodes[task.id]
        parent = nodes.get(task.parent_id) if task.parent_id is not None else None
        if parent is None or parent is node:
            roots.append(node)
        else:
            parent.children.append(node)

    _sort(roots)
    _assign_depth(roots, 0)
    return roots


def _sort(nodes: list[TaskNode]) -> None:
    nodes.sort(key=lambda node: (node.task.position, node.task.id))
    for node in nodes:
        _sort(node.children)


def _assign_depth(nodes: list[TaskNode], depth: int) -> None:
    if depth > MAX_DEPTH:
        return
    for node in nodes:
        node.depth = depth
        _assign_depth(node.children, depth + 1)


def flatten(nodes: list[TaskNode]) -> list[TaskNode]:
    """Depth-first order, for rendering the tree as a flat indented list.

    Templates iterate a flat sequence and indent by `depth`, which avoids a
    recursive Jinja macro for what is really just a list with a margin.
    """
    out: list[TaskNode] = []
    for node in nodes:
        out.append(node)
        out.extend(flatten(node.children))
    return out


def would_create_cycle(task: Task, new_parent: Task) -> bool:
    """True if reparenting `task` under `new_parent` closes a loop.

    Walks up from the proposed parent looking for the task itself. Bounded by
    MAX_DEPTH so an already-corrupt tree cannot hang the request.
    """
    if task.id == new_parent.id:
        return True

    current: Task | None = new_parent
    for _ in range(MAX_DEPTH):
        if current is None:
            return False
        if current.id == task.id:
            return True
        current = current.parent
    return True


@dataclass(frozen=True)
class ReadyTask:
    """A leaf task one click away from running code.

    Two cases produce one of these: a plan run is `awaiting_review` with
    nothing left to decide — approving it starts execute directly, or creates
    the subtasks it proposed — or a task has never run at all but a parent's
    plan already marked it `entry_phase=execute`, fully specified with
    nothing left to plan. Both already have a one-click button in the tree;
    this is what promotes them to the top of the page.
    """

    node: TaskNode
    #: Set only for the "approve a plan" case — posts to `/runs/{id}/approve`.
    #: `None` for the "never run, already specified" case, which instead
    #: posts to `/tasks/{id}/runs`.
    run_id: int | None
    #: `Run.plan` for the approval case, or the task's own body — the closest
    #: thing to a plan a never-run task has — for the other.
    plan_text: str | None
    proposed_subtask_count: int

    @property
    def plan_preview(self) -> str | None:
        """`plan_text`, cut to a skimmable length — mirrors `body_preview`."""
        if not self.plan_text:
            return None
        if len(self.plan_text) <= BODY_PREVIEW_CHARS:
            return self.plan_text
        return _truncate_at_word(self.plan_text, BODY_PREVIEW_CHARS)

    @property
    def plan_rest(self) -> str | None:
        """Whatever `plan_preview` left out — mirrors `body_rest`."""
        text = self.plan_text
        preview = self.plan_preview
        if not text or preview is None or len(text) <= BODY_PREVIEW_CHARS:
            return None
        return text[len(preview) :].lstrip()


def ready_to_execute(
    nodes: list[TaskNode],
    activity: dict[int, TaskActivity],
    pr_urls: dict[int, str],
    checkout: bool,
) -> list[ReadyTask]:
    """Leaf tasks a single click away from starting or continuing execution.

    Deliberately narrower than "every task with a run button": a task that
    still needs its first *plan* is ready to be planned, not ready to
    execute, so it is left out even though the tree also offers it a button.
    A task with anything else in flight (queued, running, or failed) is left
    out too — something is already happening, or a different action (retry)
    applies, neither of which is "one click starts execution".
    """
    ready: list[ReadyTask] = []
    for node in nodes:
        if not node.is_leaf:
            continue
        if node.effective_status in (TaskStatus.DONE, TaskStatus.CANCELLED):
            continue
        if pr_urls.get(node.task.id):
            continue

        busy = activity.get(node.task.id)
        if busy is not None:
            if busy.needs_attention and busy.phase is RunPhase.PLAN:
                ready.append(
                    ReadyTask(
                        node=node,
                        run_id=busy.run_id,
                        plan_text=busy.plan,
                        proposed_subtask_count=busy.proposed_subtask_count,
                    )
                )
            continue

        if checkout and node.task.entry_phase is RunPhase.EXECUTE:
            ready.append(
                ReadyTask(
                    node=node,
                    run_id=None,
                    plan_text=node.task.body,
                    proposed_subtask_count=0,
                )
            )
    return ready
