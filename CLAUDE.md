# Workbench

A personal tool for managing software projects on a home server. Lists projects,
shows a todo tree per project, and lets a task be worked on either by hand or by a
Claude agent — with a written summary either way.

Runs on an always-on Ubuntu box, reachable only over Tailscale.

> **Status: early.** Users and their GitHub-backed projects exist, served by a FastAPI
> app installed with `./install.sh`, which then keeps itself up to date from `main`. The
> tables for tasks, runs, and run events exist, but nothing reads or writes them yet —
> the schema landed ahead of the code so that the deploy pipeline had something real to
> migrate. Worktrees and agents do not exist, and **no agent backend is chosen or
> depended on**: there is nothing vendor-specific in the repo. See `README.md` for what
> is actually live.

## Reproducibility is a project goal

A fresh Ubuntu Server 26.04 machine, a `git clone`, and `./install.sh` must produce a
running service. Nothing else. If a step is needed, it belongs in that script rather
than in a document — the bar is that this repo could be handed to someone with a spare
machine and work without a conversation.

Three consequences that shaped real decisions:

- **Alembic from the start**, not `create_all()`. The alternative meant "delete the
  database when the schema changes", which is exactly the kind of undocumented manual
  step this rule exists to prevent.
- **Application data lives in `data/` inside the repo**, not `/var/lib/workbench`. An
  external path has to be created, permissioned, and remembered, and none of that
  survives a clone onto a new machine. It is gitignored, so pulling never touches it.
- **Updating is automated too, not just installing.** The same rule applied to the second
  install onward: "ssh in, pull, re-run the installer, remember to migrate" is a manual
  step, and a schema step that gets skipped is the one that corrupts something. A timer
  does it instead — see Deploying below.

The claim is kept honest by `scripts/test_fresh_install.py`, which provisions a clean
Ubuntu container, runs the install, drives the app over HTTP, and re-runs the install to
prove it repeats. A setup step that creeps outside `install.sh` makes that test fail.

Two things are deliberately *not* automated, and are decisions rather than oversights:

- **Joining a Tailscale network.** It needs a browser login against an account the
  script cannot know about. `install.sh` finishes with a working service on localhost
  and prints the two commands to expose it.
- **A dedicated `workbench` service user.** The service currently runs as whoever ran
  the installer. An unprivileged account only starts bounding anything once agents are
  executing model-authored shell commands, so it belongs to that slice.

## Decisions already made

**Language: Python.** FastAPI + uvicorn, SQLite via SQLAlchemy, SSE for streaming agent
output to the browser. Ubuntu 26.04 enforces PEP 668, so everything installs into a venv
rather than system Python.

**The server has no Node, and keeping it that way is a constraint on backend choice**
rather than a consequence of one. A backend that needs a Node runtime on the box has to
justify it. The server's Python is 3.14, new enough that wheels are not guaranteed for
everything — worth checking in a throwaway venv before committing to any SDK.

**The agent backend is swappable, and the schema assumes so.** Claude is the first
implementation, not the interface. Nothing in the data model is named for a vendor, and
three things exist specifically to keep a later switch cheap:

- **`runs.backend` and `runs.model`** record what actually ran each attempt. This is the
  one column that genuinely cannot be backfilled — the moment a second backend exists,
  every earlier row is ambiguous without it. `projects.agent_backend` overrides the
  machine-wide `WORKBENCH_AGENT_BACKEND` default per project.
- **`runs.resume_token` is opaque.** It is never parsed, and it means nothing to a
  backend other than the one that issued it, which is why it is always read together
  with `runs.backend`. Named for what it does rather than after any SDK's "session".
- **`RunEventKind` is Workbench's vocabulary**, not a passthrough of whatever an SDK
  emits: `text`, `thinking`, `tool_use`, `tool_result`, `status`, `notice`. Backends
  translate into it. This is what keeps a run recorded a year ago readable after a
  switch, and stops two backends spelling the same thing two ways. It only grows when a
  *reader* needs a new distinction; anything else is a `notice`.

The remaining coupling is in the code that has not been written yet. When the agent
slice lands, the SDK import belongs behind one adapter that yields `RunEventKind` events
and an opaque resume token — not scattered through the runner.

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

## Deploying is automatic, and pull-based

**Merging to `main` is the deployment.** `workbench-deploy.timer` fires every five minutes;
`workbench-deploy.service` fetches, fast-forwards, syncs, migrates, reinstalls any changed
unit, and restarts the app. There is no server step after a merge, and in particular no
schema step to forget.

**Pull, not push, and this is forced rather than chosen.** The server has no public
ingress — tailnet only, and `tailscale funnel` is ruled out above — so GitHub webhooks
cannot reach it and neither can a GitHub Actions runner. A self-hosted runner would work
but means parking a long-lived registration credential on the box and letting workflow
code execute there. Polling needs no inbound path, no secret, and no new daemon. The cost
is latency: a merge lands within the timer interval instead of instantly.

**It only ever fast-forwards.** A checkout that is dirty, on another branch, or carrying a
local commit is reported into the journal and left untouched, so working on the server by
hand is never interrupted and nothing is discarded. A migration failure stops the deploy
*before* the restart, leaving the old code running against the schema it matches — a
failed deploy should not become an outage.

**The deployer runs as root, the app does not.** It needs to restart the unit, so it drops
to the checkout's owner with `runuser` for every command touching git, the virtualenv, or
the database. Doing that work as root would leave root-owned files that the unprivileged
service could no longer write. For the same reason `install.service_user()` reads the
*checkout's owner* rather than the current euid — reading the euid would silently
re-render the unit with `User=root` on the first automatic deploy.

### Known trap: self-deployment

Workbench is intended to eventually be developed inside Workbench, and a task ending in
"restart the workbench service" would kill the process serving the request.

**Half of this is now handled.** Deploys run from their own systemd unit, so the restart
they trigger lands on a different cgroup and cannot kill the deployer — and since the
timer owns deployment, nothing has to reach back into the app to trigger one at all.

The other half is still open, and is the broader problem: if agent runs live inside the
uvicorn process, *any* restart — deploy, crash, OOM — orphans them, and a deploy now
happens on a timer without anyone watching. Agents should therefore run as detached child
processes writing to a durable event log, with the web tier as a pure reader. That is a
prerequisite for the runs slice rather than a nicety, because automatic deploys make the
orphaning routine instead of occasional.

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
