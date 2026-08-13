# Workbench

A personal tool for managing software projects on a home server. Lists projects,
shows a todo tree per project, and lets a task be worked on either by hand or by a
Claude agent — with a written summary either way.

Runs on an always-on Ubuntu box, reachable only over Tailscale.

> **Status: nothing is built yet.** The repo currently contains this design doc, the
> server conventions in `docs/server-conventions.md`, and a static placeholder page in
> `www/`. See `README.md` for what is actually live.

## Decisions already made

**Language: Python.** FastAPI + uvicorn, SQLite via the stdlib `sqlite3`, SSE for
streaming agent output to the browser. The `claude-agent-sdk` package is a pure Python
package, so the server needs no Node install — the server has no Node and we intend to
keep it that way. Two caveats to verify before relying on this (see Open questions):
the SDK may require the Claude Code CLI on `PATH` separately, and the server's Python is
3.14, new enough that wheels for the SDK and its dependencies are not guaranteed.
Ubuntu 26.04 enforces PEP 668, so everything installs into a venv rather than system
Python.

**Tasks live in local SQLite, not GitHub Issues.** GitHub is used for code hosting,
remotes, and PRs only. Issues were considered and rejected: every UI interaction
becomes a network round-trip, and the sub-issue API is an awkward fit for the
"a task can grow or gain children" behavior this tool is built around. Syncing to
Issues later is possible if mobile visibility turns out to matter; the schema is
ours either way.

**Nothing runs in the cloud.** The agent has to execute in a checkout of the repo,
and the repos are on this machine. Cloud hosting would mean either shipping code out
or letting something reach back in. AWS was considered and rejected on fit, not cost.

**One git worktree per task.** This is the load-bearing idea. Starting a task —
manually *or* with an agent — creates a branch and a worktree; the agent runs with
`cwd` set to that worktree, and manual work happens by `cd`-ing there. Consequences:

- Two tasks can be in flight without stomping each other.
- Manual and agent work produce the identical artifact (a branch with commits), so
  "summarize what I did" and "summarize what Claude did" are one code path.
- An interrupted session still summarizes usefully, because the summarizer is fed
  both committed and uncommitted diffs.

**Data model.** `projects` (name, repo_path, github_repo, base_branch) → `tasks`
(self-referencing `parent_id` for the tree, status open/active/done) → `runs`
(one row per attempt: mode agent|manual, branch, worktree_path, session_id, summary,
diffstat). Storing the SDK `session_id` on the run is what allows resuming an agent
conversation on a task later.

**Workbench runs as a systemd unit, not a container.** The server's general convention
is Docker Compose for self-contained services, but Workbench's job is to manipulate the
host — worktrees, build tools, `systemctl` — so it runs natively. The full rationale is
in `docs/server-conventions.md`.

## Deployment

- systemd unit, `Restart=always`, logs to journald. systemd 259 supports
  `ProtectSystem=strict` with an explicit `ReadWritePaths=` allowlist, which is
  preferable to disabling hardening wholesale — the agent legitimately needs to write to
  repos and run build tools, but it does not need the whole filesystem.
- Runs as a dedicated unprivileged `workbench` user with no sudo. A headless agent
  with pre-approved permissions executes model-authored shell commands; the separate
  account bounds the blast radius to files recoverable from GitHub.
- Secrets in `/etc/workbench/env` (mode 0600, owned by the service user), loaded via
  `EnvironmentFile`. Alternatively authenticate the bundled CLI once as that user to
  bill against a Claude subscription instead of the API. Either way the credential is
  readable by model-authored shell commands running as that user — inherent, but worth
  stating.
- The service user needs its own SSH deploy key and `user.name`/`user.email`, or
  unattended pushes and agent commits will fail. Prefer per-repo deploy keys or a
  fine-grained PAT over an account-wide key, which would grant push to every repo.
- App binds `127.0.0.1`; `tailscale serve --bg http://127.0.0.1:8787` terminates TLS
  and publishes it at `https://homebox-core.tail4c4cf3.ts.net`. Requires MagicDNS and
  HTTPS Certificates enabled in the Tailscale admin console. Valid HTTPS is what makes
  "Add to Home Screen" behave like a real app on a phone. Never `tailscale funnel` —
  that exposes it publicly.
- **There is no auth at the app layer.** This is only acceptable because the server sits
  on a two-device personal tailnet. `tailscale serve` publishes to the entire tailnet,
  and this app is remote code execution by design. Revisit before joining the server to
  any shared tailnet.
- Backups: only the SQLite file is irreplaceable (repos are on GitHub, worktrees are
  disposable). Use `sqlite3 workbench.db ".backup ..."` on a timer, not `cp` — WAL
  mode makes a naive copy of a live database unsafe. Then restic/borg offsite. The
  server is a single volume, so the offsite leg is the only real backup. Note that
  agent session transcripts live in the service user's home and are on neither GitHub
  nor the SQLite file.

## Known trap: self-deployment

Workbench is intended to eventually be developed inside Workbench. A task ending in
"restart the workbench service" kills the process serving the request, so the run
never gets marked finished and its summary is lost. Deploys must go through a
detached one-shot (`workbench-deploy.service`) that the app triggers with
`systemctl start`, rather than restarting itself.

This is a special case of a broader problem: if agent runs live inside the uvicorn
process, *any* restart — deploy, crash, OOM — orphans them. Consider running agents as
detached child processes that write to a durable event log, with the web tier as a pure
reader. That makes the web tier freely restartable and mostly dissolves the trap.

## Open questions

Unresolved. Recorded here so they are not rediscovered later.

- **Does `claude-agent-sdk` work on Python 3.14?** And does it require the Claude Code
  CLI installed separately on `PATH`? The whole language choice rests on this. Test in a
  throwaway venv on the server before committing.
- **One worktree per task, or per run?** The prose above says per task; the data model
  puts `branch` and `worktree_path` on `runs`. These are different systems — per-run
  gives each attempt a clean branch, per-task lets a second run resume in place. Pick one
  and make both halves agree.
- **Agent sessions are directory-scoped.** The SDK exposes `list_sessions(directory=...)`
  and keys sessions to the cwd they ran in. That conflicts with "worktrees are
  disposable" — deleting a worktree may orphan the `session_id` a run points at.
- **SSE has no replay.** A phone that sleeps mid-run silently loses everything emitted
  during the gap. Likely fix: persist every agent event as it arrives and have the SSE
  endpoint replay from `Last-Event-ID` before tailing live. Decide before building the
  streaming layer; retrofitting means rewriting it.
- **A fresh worktree is not a working checkout.** `git worktree add` gives tracked files
  only — no `.env`, no `node_modules`, no venv. Most agent runs will fail their first
  build for reasons unrelated to the task. Needs a per-project setup command and/or a
  list of gitignored paths to link in, which the `projects` schema has nowhere to put.
- **Blocking `sqlite3` in async handlers** will stall the event loop under a long run.
  Either make handlers `def` or use `run_in_threadpool`.
- **Nothing caps concurrency.** Three taps starts three agents, each running builds.
- **Store token/cost per run.** Cheap now, painful to backfill.
- **`open/active/done` has nowhere to put a failed or cancelled run.** Also undecided:
  whether a parent task's status is derived from its children or independent.
- **Large diffs will blow the summarizer's context.** Needs diffstat-first truncation.

## Deferred

- Recurring tasks and scheduled agents. The original motivation, but it needs the
  task/run loop working first. Lean toward an in-process scheduler over systemd timers —
  the app is always-on anyway, and a timer would need to reach back in over HTTP to hit
  the same "start a run" code path.
- Cross-project "what should I work on next" view.
- Optional GitHub Issues sync.
