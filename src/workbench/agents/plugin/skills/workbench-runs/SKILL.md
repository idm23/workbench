---
name: workbench-runs
description: Use this when running as Workbench's conversation phase (the project chat, or a task's own Discuss conversation), to start a plan or execute run on a task when the person you're talking with explicitly asks you to, for that task, right now. Applies whenever the environment has WORKBENCH_API_BASE set.
version: 1.0.0
---

# Starting a run on a Workbench task

You are talking directly with a person — either the project's own chat, or a
conversation about one task in particular. The base URL to reach Workbench's
own API is in your environment as `$WORKBENCH_API_BASE` (always `127.0.0.1`,
on this same machine — nothing here leaves the box). There is no
authentication on this route; being reachable at all is the only check.

## Start a run

```
curl -sf -X POST "$WORKBENCH_API_BASE/api/tasks/{task_id}/runs" \
  -H 'Content-Type: application/json' \
  -d '{"phase": "plan"}'
```

`phase` is `"plan"` (investigate and propose; change nothing) or `"execute"`
(carry out an approved plan, or make the change directly for a task simple
enough to skip planning). Both are exactly what the task tree's own Plan and
Execute buttons do — this is the same action, reached from the conversation
instead of a tap.

On success (`201`) you get the created run back as JSON — `id`, `task_id`,
`phase`, `status`, `backend`. Tell the person a run started, and its id.

On failure you get a `4xx` with `detail` saying why: the task already has a
run going, the concurrency cap is full, the task has sub-tasks and so has no
single thing to run, or the agent named is not one this project allows.
Relay that reason to the person rather than retrying or working around it —
every one of these is the system refusing on purpose, the same way the
button would refuse it.

Two optional fields, almost never needed: `origin` (which branch to start
the task's worktree from — only matters before it has one) and `agent`
(`backend` or `backend:login`, to use something other than the project's
default or, for a task run before, whatever it last used).

## When to call this, and when not to

**Only when the person you are talking with, in this conversation, has just
asked you to run a specific task.** "Plan the auth task", "go ahead and
execute #42", "retry that one" — all fine. Do not decide on your own to
start a run on a task nobody mentioned, and do not use one request to kick
off several tasks at once unless they explicitly asked for that. Starting a
run is not like reading or editing the task list: it spends a concurrency
slot and may end up opening a pull request, which is not something to do
unattended on a guess.

A run you start keeps going after this conversation does — you will not see
what it reports here. If you are asked later whether it finished, re-read
the project's tasks (the same `GET /api/projects/{project_id}/tasks` the
workbench-tasks skill uses) and find this one's `latest_run`: its `status`,
and once it is done, its `summary` and `pr_url`.
