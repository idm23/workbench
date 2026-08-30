"""What Workbench asks the agent to do, in each phase.

These belong to Workbench rather than to any backend. `RunPhase` is this
project's two-step workflow — plan, have a person read it, then carry it out —
and a backend that has no read-only mode of its own honours the plan phase by
being asked to, which only works if the asking is written down somewhere
backend-independent. That is here.
"""

from workbench.database.models import RunPhase


def plan_prompt(title: str, body: str | None = None) -> str:
    """The planning phase: investigate, decide, change nothing.

    Deliberately does not prescribe headings for the `plan` text itself. A
    backend running this in a real plan mode already produces a structured
    plan, and demanding a format tends to yield a filled-in template rather
    than actual thought about the task. The response's overall shape (a
    `plan` string plus a `subtasks` array) is enforced separately, by the
    backend's structured output — this only has to explain what belongs in
    each field, not how to format either one.

    The instruction not to stop and ask is load-bearing rather than stylistic:
    this runs detached, on a server, with nobody attached to the process.
    """
    parts = [
        "You are working in a git worktree of this project, on a branch created "
        "for this task alone.",
        "",
        f"# Task: {title}",
    ]
    if body:
        parts += ["", body]
    parts += [
        "",
        "Investigate the codebase and produce a plan for this task. Do not make "
        "any changes yet — this is the planning phase, and a person will review "
        "your plan before anything is carried out.",
        "",
        "If the task is ambiguous, state the interpretation you are planning "
        "against rather than stopping to ask; there is nobody attached to this "
        "session to answer.",
        "",
        "If this task is genuinely better carried out as several separate "
        "pieces, list them as subtasks rather than one large plan — but do not "
        "decompose a task that does not need it; most tasks are one piece. "
        "Each subtask needs a title and a body detailed enough for whoever "
        "works it next to act on without re-reading this investigation, and "
        "your own judgement on whether it is fully specified enough to carry "
        "out directly (mark it as such) or still needs its own planning pass "
        "first (leave it for one). If no decomposition is warranted, leave the "
        "subtasks empty and let your plan stand on its own.",
    ]
    return "\n".join(parts)


def execute_prompt(title: str, body: str | None = None) -> str:
    """The execute phase: carry out the plan.

    Usually this resumes the planning conversation, and stays short for
    exactly the reason it always has — the agent already has the task, the
    codebase, and its own plan in context, and repeating them would invite it
    to re-derive what it already worked out. But a subtask a plan judged
    already fully specified starts execute cold, with no plan run behind it
    to resume, which is why `body` is included when there is one: for a
    resumed run it is a harmless, brief restatement of the original ask; for
    a cold start it is the only specification the agent has.

    It commits but does not push. Workbench pushes and opens the pull request
    itself, so that a run which fails midway leaves its work on a branch rather
    than a half-formed pull request, and so that manual and agent work produce
    the identical artifact.
    """
    parts = [f"Carry out this task now: {title!r}."]
    if body:
        parts += ["", body]
    parts += [
        "",
        "Commit your work in logical commits as you go, with clear commit "
        "messages. Do not push, and do not open a pull request — Workbench does "
        "both once you finish.",
        "",
        "When you are done, reply with a summary of what you changed and why, "
        "written for someone reviewing the pull request for "
        f"{title!r} without having watched you work. Mention anything you left "
        "undone or were unsure about.",
        "",
        "Use the workbench-outcome skill to report whether this task finished, "
        "failed, or needs re-planning before you stop — an unreported run is "
        "never assumed to have succeeded.",
    ]
    return "\n".join(parts)


def prompt_for(phase: RunPhase, title: str, body: str | None = None) -> str:
    """The prompt for a task phase, so callers switch on the enum in one
    place. `CONVERSATION` is not a task phase — see `conversation_prompt`."""
    if phase is RunPhase.PLAN:
        return plan_prompt(title, body)
    return execute_prompt(title, body)


def conversation_prompt(owner: str, repo: str) -> str:
    """The conversation phase: an open-ended chat about one project.

    Short, like execute — the mechanics of actually touching the task list
    belong to the `workbench-tasks` skill, not here. This only sets the
    scene: which project, and that managing tasks is something to actually
    do through the API when asked, not just describe.
    """
    return (
        f"You are Workbench's assistant for the project {owner}/{repo}, "
        "talking directly with the person who owns it rather than working "
        "one task in isolation.\n"
        "\n"
        "Chat naturally. When asked to look at, add, change, or clean up "
        "items on the project's task list, use the workbench-tasks skill to "
        "actually do it through the API rather than only describing what "
        "should happen.\n"
        "\n"
        "This conversation runs for a while — reply to each message and "
        "then wait for the next one rather than assuming you are finished."
    )
