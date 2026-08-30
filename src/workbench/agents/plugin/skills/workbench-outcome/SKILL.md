---
name: workbench-outcome
description: Use this when running as a Workbench execute task, to report whether the task finished, failed, or needs re-planning, and optionally to spin off a follow-up task. Applies whenever the environment has WORKBENCH_RUN_ID, WORKBENCH_TASK_ID, and WORKBENCH_API_BASE set.
version: 1.0.0
---

# Reporting a Workbench task's outcome

You are carrying out one task for Workbench, a tool that runs agents against
git worktrees and tracks the work as tasks and runs. This run's identifiers
are in your environment: `$WORKBENCH_RUN_ID`, `$WORKBENCH_TASK_ID`, and the
base URL to reach Workbench's own API, `$WORKBENCH_API_BASE` (always
`127.0.0.1`, on this same machine — nothing here leaves the box).

## Report what happened, exactly once, near the end

After you have committed your work (or decided you cannot), call:

```
curl -sf -X POST "$WORKBENCH_API_BASE/api/runs/$WORKBENCH_RUN_ID/outcome" \
  -H 'Content-Type: application/json' \
  -d '{"outcome": "finished", "detail": "optional one-line note"}'
```

`outcome` is one of:

- **`finished`** — you carried out the task as given and committed the work.
  Workbench marks the task done on this alone; only report it when the work
  is actually complete, not merely attempted.
- **`failed`** — you attempted it but hit something you could not work
  around (a broken test you could not fix, a missing dependency, a genuine
  contradiction in the instructions). Say what, briefly, in `detail`.
- **`needs_replanning`** — the instructions turned out to be wrong,
  ambiguous, or bigger than they looked, and the right next step is for a
  person to review and re-plan rather than for you to keep guessing. Say why
  in `detail`.

If you do not call this at all, Workbench treats the run as unreported —
it will **not** assume you succeeded, so it costs you nothing to skip if
you genuinely have nothing to say, but a `failed` or `needs_replanning` you
fail to report is a task that silently looks fine to whoever checks on it
next. When in doubt, report.

## Spinning off a follow-up task

If, while working or while explaining a `needs_replanning` outcome, you
identify a specific, separate piece of work worth tracking on its own
(rather than folding it into your own summary), create it as a subtask of
the task you are working:

```
curl -sf -X POST "$WORKBENCH_API_BASE/api/tasks/$WORKBENCH_TASK_ID/subtasks" \
  -H 'Content-Type: application/json' \
  -d '{"title": "short title", "body": "what needs doing and why", "ready_to_execute": false}'
```

Set `"ready_to_execute": true` only when the subtask is fully specified —
enough that an agent could carry it out directly with no planning pass of
its own. Leave it `false` (or omit it) when it still needs investigation
first. This is optional and independent of the outcome report above; use it
when it genuinely clarifies the work, not as a substitute for finishing what
you were asked to do.
