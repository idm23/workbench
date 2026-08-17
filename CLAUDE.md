# Workbench

A personal tool for managing software projects on a home server. Lists projects,
shows a todo tree per project, and lets a task be worked on either by hand or by a
Claude agent — with a written summary either way.

Runs on an always-on Ubuntu box, reachable only over Tailscale.

> **Status: early.** Users, GitHub-backed projects, a task tree per project, and agent
> runs that plan, execute, and open a pull request. Served by a FastAPI app installed
> with `./install.sh`. See `README.md` for what is actually live.

## Reproducibility is a project goal

A fresh Ubuntu Server 26.04 machine, a `git clone`, and `./install.sh` must produce a
running service. Nothing else. If a step is needed, it belongs in that script rather
than in a document — the bar is that this repo could be handed to someone with a spare
machine and work without a conversation.

Two consequences that shaped real decisions:

- **Alembic from the start**, not `create_all()`. The alternative meant "delete the
  database when the schema changes", which is exactly the kind of undocumented manual
  step this rule exists to prevent.
- **Application data lives in `data/` inside the repo**, not `/var/lib/workbench`. An
  external path has to be created, permissioned, and remembered, and none of that
  survives a clone onto a new machine. It is gitignored, so pulling never touches it.

The claim is kept honest by `scripts/test_fresh_install.py`, which provisions a clean
Ubuntu container, runs the install, drives the app over HTTP, and re-runs the install to
prove it repeats. A setup step that creeps outside `install.sh` makes that test fail.

Four things are deliberately *not* automated, and are decisions rather than oversights.
The first three are credentials or logins a script cannot supply; `install.sh` prints all
of them on completion, so they are visible steps rather than hidden ones.

- **Joining a Tailscale network.** It needs a browser login against an account the
  script cannot know about. `install.sh` finishes with a working service on localhost
  and prints the two commands to expose it.
- **Signing in to Claude.** `claude`, once, as the service user. Agent runs bill against
  that account. It needs a TTY and an account the machine cannot know about.
- **The GitHub token.** A fine-grained PAT in `/etc/workbench/env`, scoped to the repos
  you want touched, with `contents:write` and `pull_requests:write`. A secret cannot live
  in a public repo.
- **A dedicated `workbench` service user.** The service still runs as whoever ran the
  installer. This is now the most overdue of the four: agents execute model-authored
  shell commands as that user, and both credentials above are readable by them.

Everything except agent execution works without any of these. A fresh clone installs,
serves, and manages tasks with no secret configured — which is what keeps
`scripts/test_fresh_install.py` honest.

## Decisions already made

**Language: Python.** FastAPI + uvicorn, SQLite via SQLAlchemy, SSE for streaming agent
output to the browser. Ubuntu 26.04 enforces PEP 668, so everything installs into a venv
rather than system Python.

The `claude-agent-sdk` question this rested on is **settled**: version 0.2.139 installs
cleanly on Python 3.14 and **bundles its own native `claude` binary**, which
`_find_cli()` prefers over anything on `PATH`. Verified by running a query under
`env -i PATH=/usr/bin:/bin`. So the server needs neither Node nor a separately installed
CLI, and the server has neither. The cost is size: that binary is ~310 MB, which
dominates both `uv sync` on the server and every `fresh-install` CI run.

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

**Per task, not per run** — this was an open question and is now decided. A plan run and
the execute run that follows it are two attempts at one piece of work, and the second
resumes the first's agent session, which the SDK scopes to the directory it ran in.
Per-run worktrees would invalidate that session on every approval. So `branch` and
`worktree_path` live on `tasks`.

**Two-phase runs: plan, then execute.** A run is started in `permission_mode='plan'`,
which the CLI enforces — the agent cannot edit files in that phase even if it decides it
should. It stops at `awaiting_review` with a plan. On approval a second run resumes the
same `session_id` in `acceptEdits`, commits, and Workbench pushes and opens the pull
request. Resuming rather than re-prompting is what makes the execute phase cheap: in
testing it took 2 turns against the plan phase's 10, because the agent still had the
codebase context and its own plan in the conversation.

**Data model.** `projects` (owner, repo, github_url, default_branch, local_path,
setup_command) → `tasks` (self-referencing `parent_id` for the tree, status, branch,
worktree_path) → `runs` (one row per attempt: phase plan|execute, status, session_id,
pid, plan, summary, diffstat, pr_url, cost) → `run_events` (every message from a run,
persisted as it arrives).

**Agents run as detached processes; the web tier only reads.** `python -m
workbench.runner <run_id>` is spawned with `start_new_session=True` and writes each SDK
message to `run_events` before moving on. Nothing about a run lives in the uvicorn
process. This is the general form of the self-deployment trap below, and it also gives
SSE replay for free — see Open questions, where both used to live.

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
"restart the workbench service" kills the process serving the request, so the run would
never get marked finished and its summary would be lost.

**The general form of this is now fixed.** Agent runs execute in detached processes
(`python -m workbench.runner <run_id>`, spawned with `start_new_session=True`) that write
every message to `run_events` as it arrives. The web tier is a pure reader. Restarting
uvicorn — for a deploy, a crash, or an OOM kill — does not touch a run in flight, and
reconnecting replays the output that arrived during the gap.

Two things this does *not* cover:

- A deploy still needs a detached one-shot (`workbench-deploy.service`) triggered with
  `systemctl start`, because `install.sh` restarts the service and would otherwise kill
  the request mid-flight. Not built yet.
- A runner killed outright (OOM, `kill -9`) cannot record its own outcome. `reap_stale_runs`
  notices the pid is gone on the next page load and marks the run failed, rather than
  leaving the task permanently busy.

## Open questions

Unresolved. Recorded here so they are not rediscovered later.

- **Is "no commits" a failure?** A run where the agent correctly concludes nothing needs
  changing is currently marked `failed`, because it produced no pull request. That reads
  as a malfunction when it was good judgement, but `succeeded` would imply a PR that does
  not exist. Probably wants a third outcome.
- **Agent sessions are directory-scoped.** The SDK keys sessions to the cwd they ran in,
  which conflicts with "worktrees are disposable" — deleting a task's worktree orphans
  the `session_id` its runs point at. Per-task worktrees narrow this but do not close it.
  The SDK exposes a pluggable `SessionStore` (`append`/`load`/`delete`/`list_sessions`),
  so sessions could live in our own SQLite instead of on disk. Not done yet.
- **`setup_command` is per project, but the need is per worktree.** A project whose setup
  is "symlink `.env` and the venv from the main checkout" cannot express that as one
  command without knowing the source path. May need a list of gitignored paths to link.
- **Nothing bounds an agent's blast radius.** It runs as the service user with
  `acceptEdits` and can read both credentials. The concurrency cap limits how many, not
  what each can do. This is what the dedicated `workbench` user is for.
- **A cancelled run leaves the worktree dirty**, and the next run on that task resumes
  into it. Probably right — the work is still there — but it is untested and it means
  "cancel" does not mean "undo".
- **Event log growth is unbounded.** Every tool call of every run is a row, kept forever.
  Fine now; wants pruning before it is not.
- **Whether a parent task's status should derive from its children.** Currently
  independent, with a `2/5` progress count shown instead. Deriving it would stop a parent
  being closed while a child lingers, which may be the more useful behaviour.

### Resolved by the tasks-and-runs slice

Kept briefly, because the reasoning is load-bearing and the questions were real.

- **`claude-agent-sdk` on Python 3.14, and the CLI dependency.** Settled — see Language
  above. Installs cleanly, bundles its own binary, needs no Node.
- **One worktree per task, or per run?** Per task. See Decisions above.
- **SSE has no replay.** Fixed by construction: every agent message is persisted to
  `run_events` as it arrives, and the SSE endpoint replays from `Last-Event-ID` before
  tailing. A sleeping phone loses nothing.
- **Blocking `sqlite3` in async handlers.** Route handlers are sync `def`, which FastAPI
  runs in a threadpool. The one `async def` endpoint is the event stream, which reaches
  the database through `asyncio.to_thread`.
- **Nothing caps concurrency.** `WORKBENCH_MAX_CONCURRENT_RUNS`, default 2.
- **Store token/cost per run.** `ResultMessage` reports `total_cost_usd`, `usage`, and
  `num_turns`; all three are stored.
- **`open/active/done` has nowhere for a failed or cancelled run.** Task statuses are now
  open/active/blocked/done/cancelled; runs are
  queued/running/awaiting_review/succeeded/failed/cancelled.
- **Large diffs will blow the summarizer's context.** Mostly evaporated. There is no
  separate summarizer — the agent's own final message is the summary, written while it
  still has the context. Only the diffstat is stored, which is bounded by file count.

## Deferred

- Recurring tasks and scheduled agents. The original motivation, but it needs the
  task/run loop working first. Lean toward an in-process scheduler over systemd timers —
  the app is always-on anyway, and a timer would need to reach back in over HTTP to hit
  the same "start a run" code path.
- Cross-project "what should I work on next" view.
- Optional GitHub Issues sync.
