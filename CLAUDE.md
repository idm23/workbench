# Workbench

A personal tool for managing software projects on a home server. Lists projects,
shows a todo tree per project, and lets a task be worked on either by hand or by a
Claude agent — with a written summary either way.

Runs on an always-on Ubuntu box, reachable only over Tailscale.

> **Status: early.** Users and their GitHub-backed projects exist, served by a FastAPI
> app installed with `./install.sh`, which then keeps itself up to date from `main`. The
> task tree is live: tasks nest, complete, and delete from a phone. A project can be
> cloned to the server, and `git/worktrees.py` can give a task its own worktree — though
> nothing calls it yet, because that is what runs are for. `runs` and `run_events` exist
> as tables with no reader. **No agent backend is chosen or depended on**: there is
> nothing vendor-specific in the repo. See `README.md` for what is actually live.

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

**Data model**, as built rather than as sketched:

```
users     → projects → tasks → runs → run_events
```

- **`projects`** — owner, repo, github_url, default_branch, plus `setup_command` and
  `agent_backend`. Deliberately *not* the clone's path: that is derived from owner and
  repo by `git.worktrees.local_checkout()`, because this database is copied between
  instances. Staging restores production's snapshot on every deploy, so a stored absolute
  path arrived in staging still pointing at production's checkout — and once runs exist,
  that means staging creating worktrees inside production's repository.
- **`tasks`** — self-referencing `parent_id` for the tree, `position` for sibling order,
  and `branch`/`worktree_path`, which live here rather than on runs because a plan run and
  the execute run after it share one checkout.
- **`runs`** — one row per attempt: `phase` (plan|execute), `status`, `backend`, `model`,
  `resume_token`, `pid`, `plan`, `summary`, `diffstat`, `pr_url`, `total_cost_usd`,
  `num_turns`.
- **`run_events`** — every message a run emits, unique on `(run_id, seq)`, in Workbench's
  own `RunEventKind` vocabulary. This is what makes a run survive a restart and an SSE
  stream replayable.

The tables exist and are migrated; nothing reads or writes `tasks`, `runs`, or
`run_events` yet. The schema landed ahead of the code so the deployment pipeline had
something real to migrate.

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

**It refuses rather than reconciles.** A checkout that is dirty, on another branch, or
carrying a local commit is reported into the journal and left untouched, so working on the
server by hand is never interrupted and nothing is discarded.

Uncommitted work is caught by an explicit `git status` check, not by `git merge --ff-only`.
Relying on the merge was the original design and it was wrong: git only refuses a
fast-forward that would *overwrite* a modified file, so a commit touching any other path
sails over someone's working state. That made "is my work safe" a function of what the
incoming commit happened to contain — not something anyone can reason about from the
server. Found by the deploy-cycle test, whose incoming commit deliberately touches nothing
the checkout had edited.

A migration failure stops the deploy *before* the restart, leaving the old code running
against the schema it matches — a failed deploy should not become an outage.

**Required CI checks are now load-bearing.** A merge reaches the server within five
minutes with nobody watching, so branch protection is the only thing standing between a
red build and a restarted service. Turning the timer on and leaving `main` unprotected
would be the actual risk here, not the automation itself.

### Staging, and why promotion stays manual

`staging` is a second install on the same box — its own units, port 8788, its own `data/`
— deployed by the same timer from the `staging` branch. Two things make it worth having
rather than just running CI twice:

- **It migrates a snapshot of production before every deploy.** A migration that passes
  against empty tables routinely fails against real rows, and this is the only place that
  is caught before production. The snapshot uses SQLite's backup API, never a file copy,
  because production is live and in WAL mode.
- **It is reachable from a phone**, so "does this actually feel right" is answerable
  before promoting rather than after.

Acceptance runs automatically after each staging deploy and POSTs a `staging-acceptance`
commit status. That call is **outbound**, which is what lets the whole flow work without
exposing the server — GitHub cannot ask how staging went, so the server tells it. The same
no-ingress constraint that forced polling shapes this too. Branch protection on `main`
requires that status, so a commit that never ran on staging cannot be promoted.

**Merging is deliberately a human action.** The status goes green on its own; nothing
merges itself. This tool's purpose is running agents that write code, and agent-authored
changes will be the main thing flowing through this pipeline — a person looking before it
reaches the machine they depend on is worth the click. Auto-merge is one GitHub setting
away once the acceptance suite has earned that trust.

**A failed acceptance is not a failed deploy.** Staging keeps the code it just installed,
because that is the state someone needs in order to go and look at what broke. What it
does instead is post red, which is what stops promotion.

**It cannot install itself.** A machine with no timer never checks for the commit that
would give it one, so `install.sh` gets run by hand exactly one last time on any server
that predates this. Worth noting because it is the one place the reproducibility rule
bends: a *fresh* clone gets the timer from the first install, and only an existing
deployment needs the manual step.

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

- **Agent sessions are directory-scoped.** A backend's resume token is keyed to the
  directory it ran in, which conflicts with "worktrees are disposable" — deleting a
  task's worktree orphans the `resume_token` its runs point at. Per-task worktrees narrow
  this but do not close it. Some SDKs expose a pluggable session store, which would let
  those live in our own SQLite instead of on disk.
- **SSE has no replay yet.** The `run_events` table exists precisely so the stream can be
  replayed from `Last-Event-ID` before tailing live, but nothing writes to or reads from
  it — that arrives with the runner. A phone that sleeps mid-run would currently lose
  everything emitted during the gap.
- **Nothing caps concurrency.** Three taps starts three agents, each running builds.
  `config.py` has no `max_concurrent_runs` yet.
- **`setup_command` is per project, but the need is per worktree.** A project whose setup
  is "symlink `.env` and the venv from the main checkout" cannot express that as one
  command without knowing the source path.
- **Nothing bounds an agent's blast radius.** It will run as the service user with
  whatever permissions the backend is given, and can read both credentials. This is what
  the dedicated `workbench` user is for.
- **Is "no commits" a failure?** A run where the agent correctly concludes nothing needs
  changing produced no pull request, but calling that `failed` reads as a malfunction when
  it was judgement. Probably wants a third outcome.
- **Event log growth is unbounded.** Every tool call of every run is a row, kept forever.
  Fine now; wants pruning before it is not.
- **Whether a parent task's status should derive from its children.** Currently
  independent, with a `2/5` progress count shown instead.
- **Nothing has ever failed on the real server.** Every refusal path — dirty checkout,
  crash-on-boot preflight, migration failure — is proven on CI runners and in unit tests,
  never on this machine. The first genuine bad deploy is still an unknown.

### Answered by the schema and deployment slices

Kept because the reasoning is load-bearing, and because a stale "open" question is worse
than no list at all.

- **Does the agent SDK work on Python 3.14, and does it need a CLI on `PATH`?** Tested
  against `claude-agent-sdk` 0.2.139: installs cleanly, and the wheel bundles a native
  binary that `_find_cli()` prefers over anything on `PATH`. Verified under
  `env -i PATH=/usr/bin:/bin`. So no Node and no separate install — but note this is one
  backend's answer, and the question returns for any other.
- **One worktree per task, or per run?** Per task. `branch` and `worktree_path` are on
  `tasks`, because a plan run and the execute run after it share a resume token that is
  scoped to the directory it ran in.
- **Blocking `sqlite3` in async handlers.** Route handlers are sync `def`, which FastAPI
  runs in a threadpool. The one `async def` endpoint will be the event stream, reaching
  the database through `asyncio.to_thread`.
- **Store token and cost per run.** `runs.total_cost_usd` and `runs.num_turns` exist. A
  backend that reports neither leaves them null rather than zero.
- **`open/active/done` has nowhere for a failed or cancelled run.** Task statuses are
  open/active/blocked/done/cancelled; runs are
  queued/running/awaiting_review/succeeded/failed/cancelled, with `awaiting_review` the
  one non-terminal pause.
- **A fresh worktree is not a working checkout.** `projects.setup_command` is where the
  answer goes, though see the open question above about its shape.
- **Large diffs will blow the summarizer's context.** Mostly evaporated: there is no
  separate summarizer, the agent writes its own summary while it still has the context,
  and only the diffstat is stored — bounded by file count rather than change size.

## Deferred

### Pipeline polish

Wanted, not urgent. Grouped because they are one change to how promotion works.

- **The `staging` → `main` pull request should open itself** once `staging-acceptance`
  goes green, with merging still a human action. The trigger is GitHub Actions' `on:
  status` event, which in this repository only ever fires for that status — Actions
  publishes check runs, not statuses. A pull request opened by `GITHUB_TOKEN` gets no
  fresh workflow runs, but its head is `staging`'s tip, which already carries passing
  checks from the push, and required checks are evaluated against the head commit.
  Belongs in Actions rather than in `staging_acceptance.py`: opening a pull request needs
  `pull_requests: write`, and the server's token is readable by any agent running there.
  Note `on: status` workflows only run from the *default branch's* copy of the file.
- **`main` should refuse anything not from `staging`.** Rulesets cannot express "only
  from branch X", but a required check that fails when the head is not `staging` gets
  there, and fails with a legible reason rather than sitting on a `staging-acceptance`
  that will never arrive.
- **Squash-merge into `staging`**, so each pull request is one commit there — but
  **promote to `main` with a merge commit**, never a squash or a rebase. Only a merge
  commit leaves `staging` an ancestor of `main`, which is what advances the merge base.
  Measured after the first squash promotion: the next pull request would have shown 16
  files and 1,419 lines when 12 files and 1,071 lines actually differed, and that gap
  compounds each cycle. See `docs/learning-notes.md`.
- **Keep `staging` realigned with `main`.** Mostly falls out of the point above: once
  promotion uses a merge commit, `staging` is always an ancestor of `main`, so catching
  it up is a fast-forward rather than a force push. Automating even that would need a
  bypass actor on the `staging` ruleset, because its `pull_request` rule blocks direct
  pushes from workflows too — a lot of machinery for something the merge method gives
  away free. Probably not worth it.

### Later

- Recurring tasks and scheduled agents. The original motivation, but it needs the
  task/run loop working first. Lean toward an in-process scheduler over systemd timers —
  the app is always-on anyway, and a timer would need to reach back in over HTTP to hit
  the same "start a run" code path.
- Cross-project "what should I work on next" view.
- Optional GitHub Issues sync.
