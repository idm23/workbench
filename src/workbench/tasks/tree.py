"""Assembling a flat list of tasks into the tree the page renders.

Kept separate from the routes and free of database access so the shape of the
tree — ordering, nesting, progress counts — can be tested directly. The web
layer loads rows; this decides what they look like.
"""

from dataclasses import dataclass, field

from workbench.database.models import Task, TaskStatus

#: Guards against a parent cycle rendering forever. Nothing legitimate nests
#: anywhere near this deep; hitting it means the data is corrupt.
MAX_DEPTH = 20


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
