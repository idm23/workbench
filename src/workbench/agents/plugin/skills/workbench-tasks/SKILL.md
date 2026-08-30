---
name: workbench-tasks
description: Use this when running as Workbench's project-level conversation, to read and manage the project's own task list (create, update, reparent, mark done, or delete tasks) when the person you're talking with asks for it. Applies whenever the environment has WORKBENCH_PROJECT_ID and WORKBENCH_API_BASE set.
version: 1.0.0
---

# Managing a Workbench project's task list

You are talking directly with the person who owns this project, not working
one task in isolation. This run's identifiers are in your environment:
`$WORKBENCH_PROJECT_ID`, and the base URL to reach Workbench's own API,
`$WORKBENCH_API_BASE` (always `127.0.0.1`, on this same machine — nothing
here leaves the box). There is no authentication on these routes; being
reachable at all is the only check.

When asked to look at, add, change, reorganize, or clear up the task list,
actually do it through the API below rather than only describing what
should happen.

## Read the current tree

```
curl -sf "$WORKBENCH_API_BASE/api/projects/$WORKBENCH_PROJECT_ID/tasks"
```

Returns every task as a nested tree (`children` on each one), each with
`id`, `title`, `body`, `status`, `parent_id`. Read this before making
changes that depend on what already exists — titles and ids you have not
just created are not something to guess at.

## Create a task

```
curl -sf -X POST "$WORKBENCH_API_BASE/api/projects/$WORKBENCH_PROJECT_ID/tasks" \
  -H 'Content-Type: application/json' \
  -d '{"title": "short title", "body": "detail, optional", "parent_id": null}'
```

Set `parent_id` to an existing task's id to add it as a sub-task under that
one, or omit/`null` it for a top-level task.

## Update a task

```
curl -sf -X PATCH "$WORKBENCH_API_BASE/api/tasks/{task_id}" \
  -H 'Content-Type: application/json' \
  -d '{"status": "done"}'
```

A patch — send only the fields you are changing (`title`, `body`, `status`).
`status` is one of `open`, `active`, `blocked`, `done`, `cancelled`.

## Delete a task

```
curl -sf -X DELETE "$WORKBENCH_API_BASE/api/tasks/{task_id}"
```

Deletes the task and everything under it. There is no undo — if a person
asks you to clear out a whole branch of the tree, say what you are about to
remove before doing it, the same courtesy you would use before deleting
anything else on their behalf.

## What this does not do

This skill only reads and edits the task list itself. It does not start
plans or executions against a task, and does not touch git — that is a
separate, per-task workflow the person drives from the task tree page.
Managing the list well (writing clear titles and bodies, keeping it
organized) is the actual job here.
