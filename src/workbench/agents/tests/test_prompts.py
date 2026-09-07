"""What Workbench asks for in each phase.

Prompts are usually not worth testing, but two properties here are load-bearing
rather than cosmetic: the plan phase must not invite changes, and neither phase
may tell a detached agent to stop and ask a person who is not there.
"""

from workbench.agents.prompts import execute_prompt, plan_prompt, prompt_for
from workbench.database.models import RunPhase


def test_the_plan_phase_forbids_changes():
    prompt = plan_prompt("Add a healthz endpoint")

    assert "Do not make any changes yet" in prompt.replace("\n", " ")


def test_the_task_body_is_included_when_there_is_one():
    prompt = plan_prompt("Add a healthz endpoint", "It should report the git revision.")

    assert "It should report the git revision." in prompt


def test_a_missing_body_leaves_no_empty_section():
    prompt = plan_prompt("Add a healthz endpoint", None)

    assert "\n\n\n" not in prompt


def test_a_plan_run_is_told_it_cannot_ask():
    """Not a style preference — the phase runs read-only and can call nothing,
    so asking is impossible here even now that an execute run can do it. The
    plan's job is to name the fork clearly enough that the run which *can* ask
    knows what to ask about."""
    prompt = plan_prompt("Anything")

    assert "no way to ask" in prompt
    assert "state the interpretation" in prompt


def test_an_execute_run_is_told_when_asking_is_right():
    """Stated as a bound rather than an invitation: an agent that asks about
    everything costs a person their attention every time, which is worse than
    one that decides and says so."""
    prompt = execute_prompt("Anything")

    assert "cannot settle" in prompt
    assert "changes what gets built" in prompt
    assert "is not one of those" in prompt


def test_the_plan_phase_explains_when_to_decompose():
    """The shape (a `plan` string plus `subtasks`) is enforced by structured
    output; the prompt only has to explain what belongs in each field."""
    prompt = plan_prompt("Anything")

    assert "subtasks" in prompt
    assert "most tasks are one piece" in prompt


def test_the_execute_phase_commits_but_does_not_push():
    prompt = execute_prompt("Add a healthz endpoint")

    assert "Commit your work" in prompt
    assert "Do not push" in prompt


def test_the_execute_phase_asks_for_a_summary_naming_the_task():
    assert "Add a healthz endpoint" in execute_prompt("Add a healthz endpoint")


def test_the_execute_phase_demands_an_outcome():
    """An unreported run must never read as a quiet success."""
    prompt = execute_prompt("Add a healthz endpoint")

    assert "finished, failed, or needs re-planning" in prompt
    assert "never assumed to have succeeded" in prompt


def test_the_execute_phase_names_no_backend_mechanism():
    """Two backends report an outcome two ways — a skill for one, a tool for
    the other — and this module is the one place that must know neither.
    Naming one here is how the vendor-neutral prompt quietly stops being one."""
    prompt = execute_prompt("Add a healthz endpoint")

    assert "skill" not in prompt.lower()
    assert "report_outcome" not in prompt


def test_a_cold_started_execute_task_gets_its_body():
    """A subtask a plan judged ready to execute has no plan run to resume —
    its body is the only specification the agent has."""
    prompt = execute_prompt("Add a healthz endpoint", "It should report the git revision.")

    assert "It should report the git revision." in prompt


def test_an_execute_task_with_no_body_leaves_no_empty_section():
    prompt = execute_prompt("Add a healthz endpoint", None)

    assert "\n\n\n" not in prompt


def test_prompt_for_dispatches_on_the_phase():
    assert prompt_for(RunPhase.PLAN, "T", "B") == plan_prompt("T", "B")
    assert prompt_for(RunPhase.EXECUTE, "T", "B") == execute_prompt("T", "B")
