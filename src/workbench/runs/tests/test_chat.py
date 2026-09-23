"""Several conversation runs, read as the one thread they are to the agent."""

from workbench.database.models import RunEventKind, RunPhase, RunStatus
from workbench.runs.chat import ChatMessage, SessionStart, chat_history, failed_without_a_word
from workbench.runs.store import append_event, create_conversation, create_run, finish_run


def a_session(db, project, *said):
    run = create_conversation(db, project, backend="fake")
    for kind, text in said:
        payload = {"name": text} if kind is RunEventKind.TOOL_USE else {"text": text}
        append_event(db, run.id, kind, payload)
    return run


def shown(entries):
    return [
        "--" if isinstance(entry, SessionStart) else f"{entry.kind}: {entry.text}"
        for entry in entries
    ]


def test_every_session_reads_as_one_thread_with_a_marker_between(db, task):
    project = task.project
    a_session(db, project, (RunEventKind.INPUT, "hi"), (RunEventKind.TEXT, "hello"))
    a_session(db, project, (RunEventKind.INPUT, "again"), (RunEventKind.TEXT, "still here"))

    assert shown(chat_history(db, project.id)) == [
        "--",
        "input: hi",
        "text: hello",
        "--",
        "input: again",
        "text: still here",
    ]


def test_only_what_reads_as_conversation_is_shown(db, task):
    """Thinking, results and notices are on the run's own page."""
    a_session(
        db,
        task.project,
        (RunEventKind.INPUT, "look at the tests"),
        (RunEventKind.THINKING, "hmm"),
        (RunEventKind.TOOL_USE, "Bash"),
        (RunEventKind.TOOL_RESULT, "3 passed"),
        (RunEventKind.NOTICE, "Backend fake."),
        (RunEventKind.TEXT, "They pass."),
    )

    assert shown(chat_history(db, task.project.id)) == [
        "--",
        "input: look at the tests",
        "tool_use: Bash",
        "text: They pass.",
    ]


def test_a_task_run_is_not_part_of_the_project_chat(db, task):
    run = create_run(db, task, phase=RunPhase.EXECUTE, backend="fake")
    append_event(db, run.id, RunEventKind.TEXT, {"text": "working on the task"})

    assert chat_history(db, task.project.id) == []


def test_the_newest_messages_are_kept_when_there_are_too_many(db, task):
    a_session(db, task.project, *[(RunEventKind.INPUT, f"m{n}") for n in range(5)])

    messages = [e for e in chat_history(db, task.project.id, limit=2) if isinstance(e, ChatMessage)]

    assert [m.text for m in messages] == ["m3", "m4"]


def test_a_session_that_failed_before_replying_is_reported(db, task):
    run = a_session(db, task.project, (RunEventKind.INPUT, "hi"))
    finish_run(db, run, RunStatus.FAILED, error="Not signed in.")

    failed = failed_without_a_word(db, task.project.id)

    assert failed is not None
    assert failed.error == "Not signed in."


def test_a_session_that_replied_before_failing_is_not(db, task):
    run = a_session(db, task.project, (RunEventKind.INPUT, "hi"), (RunEventKind.TEXT, "hello"))
    finish_run(db, run, RunStatus.FAILED, error="Window spent.")

    assert failed_without_a_word(db, task.project.id) is None
