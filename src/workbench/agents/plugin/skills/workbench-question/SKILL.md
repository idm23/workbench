---
name: workbench-question
description: Use this when running as a Workbench execute task and you hit a fork you genuinely cannot settle — one that changes what gets built. Asks the person and ends the run; their answer resumes this same session. Applies whenever the environment has WORKBENCH_RUN_ID and WORKBENCH_API_BASE set.
version: 1.0.0
---

# Asking the person a question

You are carrying out one task for Workbench, detached, with nobody watching the
session. Until now that meant guessing when a task turned out to be ambiguous.
It no longer does.

## When to ask, and when not to

Ask when the answer **changes what gets built** and you cannot settle it from
the code, the task, or the project's own documents. A fork where either branch
is defensible and they lead somewhere different is exactly this.

Do not ask about a detail you could pick and mention. Naming which of two
equivalent spellings you used, or which of two equally fine file layouts, costs
a person their attention for nothing — say it in your summary instead. An agent
that asks about everything is worse than one that guesses, because every
question interrupts somebody.

Look first. A question asked before reading the relevant files is not a
question, it is a reflex, and it will read as one.

## How to ask

```
curl -sf -X POST "$WORKBENCH_API_BASE/api/runs/$WORKBENCH_RUN_ID/outcome" \
  -H 'Content-Type: application/json' \
  -d '{"outcome": "needs_answer", "detail": "<your question>"}'
```

Put the whole question in `detail`, with enough context to answer it without
reading the transcript — the person may be reading it on a phone, hours later,
having forgotten the task. State the options you are choosing between and what
you would do absent an answer.

**Then stop.** Reply with a brief note of what you were doing and what you need
to know, and end your turn. The run finishes in `awaiting_answer`, holding no
concurrency slot, with your work still on its branch. Answering it from the task
tree starts a new run that resumes this session, so you will have your own
question — and everything that led to it — in context.

Ask everything you need in one question. Each one costs a round trip through a
person.

## What it is not

Not a way to report that the task is done, failed, or needs re-planning — that
is the `workbench-outcome` skill, and reporting an outcome is still required.
A question is what you send *instead* of finishing, when finishing would mean
guessing.
