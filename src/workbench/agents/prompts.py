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

    Deliberately does not prescribe headings. A backend running this in a real
    plan mode already produces a structured plan, and demanding a format tends
    to yield a filled-in template rather than actual thought about the task.

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
    ]
    return "\n".join(parts)


def execute_prompt(title: str) -> str:
    """The execute phase: carry out the plan that was just approved.

    Short because it resumes the planning conversation. The agent already has
    the task, the codebase, and its own plan in context; repeating them would
    invite it to re-derive what it already worked out.

    It commits but does not push. Workbench pushes and opens the pull request
    itself, so that a run which fails midway leaves its work on a branch rather
    than a half-formed pull request, and so that manual and agent work produce
    the identical artifact.
    """
    return (
        "Your plan has been reviewed and approved. Carry it out now.\n"
        "\n"
        "Commit your work in logical commits as you go, with clear commit "
        "messages. Do not push, and do not open a pull request — Workbench does "
        "both once you finish.\n"
        "\n"
        "When you are done, reply with a summary of what you changed and why, "
        "written for someone reviewing the pull request for "
        f"{title!r} without having watched you work. Mention anything you left "
        "undone or were unsure about."
    )


def prompt_for(phase: RunPhase, title: str, body: str | None = None) -> str:
    """The prompt for a phase, so callers switch on the enum in one place."""
    if phase is RunPhase.PLAN:
        return plan_prompt(title, body)
    return execute_prompt(title)
