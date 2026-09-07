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
> as tables with no reader. **The agent seam and the runner now exist**:
> `workbench/agents/` drives Claude behind a vendor-neutral interface, and
> `python -m workbench.runs.runner <id>` carries out a run end to end, writing every
> event to `run_events` as it happens. Runs start and stop from the task tree, each as its
> own systemd unit, and a run's page streams its output live and replays anything a
> sleeping phone missed. Every page shows how much of each rate-limit window is left.
> The installer now creates a dedicated `workbench` account, relocates the deployment to
> `/srv`, and finishes by saying what a person still has to do by hand —
> `python -m workbench.doctor` answers the same questions any time afterwards.
> **Agents now run on the real server.** The polkit grant, the per-run unit and the
> conversation path are all proven against the machine rather than a stub. What the first
> real use found was not any of those: it was the credential expiring on a clock nothing
> was watching, with every page still reporting a healthy login. See Deployment below.
> See `README.md` for what is actually live.

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

Two things are deliberately *not* automated, and are decisions rather than oversights.
Both need a browser login against an account no script can know about:

- **Joining a Tailscale network.**
- **Signing the agent in.** Under a subscription the credential is an OAuth token minted
  by an interactive login, as the service account.

The rule they bend is narrower than it looks: a step may be un-automatable, but it may
not be *undiscoverable*. So `install.sh` finishes by running `python -m workbench.doctor`
and printing each outstanding step with the exact command. `scripts/test_fresh_install.py`
asserts the login step is named in that output, which is what keeps the promise from
quietly decaying — the failure it prevents is a machine that installs and deploys
perfectly and then fails every run at authentication with nothing anywhere saying why.
Which is exactly what happened here first time.

**The dedicated `workbench` account is no longer deferred.** It is created by the
installer, and the deployment is relocated to `/srv/<service name>` and chowned to it.
That location is forced rather than chosen: Ubuntu creates home directories mode 0750,
so a checkout under a person's home is one a separate account cannot even traverse.
Keeping the checkout *owned by* the service account is what lets
`install._service_passwd()` and `deploy.repo_owner()` stay as they are — both already
read the checkout's owner, so nothing needs a `getpwnam` in the render path and no
machine lacking the account goes red.

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

That adapter now exists. `workbench/agents/` is the seam: `protocol.py` defines what
Workbench asks for and accepts back, `registry.py` turns a backend name into an
implementation, and `agents/claude.py` is the only module in the repository permitted to
import an agent SDK. It translates into `RunEventKind` and returns an opaque resume token,
so nothing above it can tell which vendor answered.

**A second backend now exists, which is the first evidence any of this works.**
`agents/local.py` drives a model served on this machine or this network through a plain
OpenAI-compatible `/chat/completions` — Ollama, `llama-server` and vLLM all speak it, so
which one is running is a URL rather than a code path. The telling detail is what it did
*not* need: no change to `test_seam.py`, because it imports `httpx` and no vendor SDK at
all. The seam constrains it exactly as it constrains the runner.

Four things it does differently, each forced rather than chosen:

- **There is no agent on the other end, only a model**, so Workbench supplies the tools
  (`agents/tools.py`), drives the turn loop, and decides when the run is over. That is the
  real cost of the backend and also its one advantage: what the agent can do is a list in
  one file rather than a vendor's decision.
- **The plan phase is read-only by absence.** Claude gets that from the SDK's plan mode;
  here the tools that write are simply not in the list sent for a plan run, and `dispatch`
  refuses one by name even if the model invents it. There is nothing to bypass, which
  makes it the stronger of the two guarantees.
- **The transcript belongs to Workbench**, written under `data/sessions/` and named by the
  opaque resume token, because a local endpoint keeps no session to resume.
- **A run bills nothing**, so `total_cost_usd` stays null rather than becoming a zero. It
  spends a GPU and a wall clock; the rate-limit panel has nothing to say about it, which
  is the entire point of having it.

**What a real small model does, and what the loop had to grow to survive it.**
None of this came from design; all four came from the first runs against
`qwen2.5-coder:7b` on the node, and each one had already produced a *wrong result
that looked right* before it was fixed.

- **It writes tool calls as prose.** Ollama's parser only recognises Qwen's
  `<tool_call>` tags, so an untagged call arrives as message content — and a loop
  that reads "no tool calls" as "finished" records the JSON as the run's summary
  and reports success. The loop now recovers a call from text, keyed on the tool
  name actually existing in that phase so a summary containing JSON is still a
  summary.
- **It composes whole scripts before seeing a result.** Read, edit, commit,
  report, in one message — with the edit written against a file it had not read.
  So a batch stops at the first failure: everything queued behind one is
  reasoning from a result that never happened.
- **It reaches a verdict without looking.** The very first run reported
  `needs_replanning` — "the specification is too vague" — on turn one, having
  read nothing. `report_outcome` and `submit_plan` now refuse to be the first
  thing a run does.
- **It claims to have finished when it has not.** A `finished` is refused while
  the worktree is exactly as the run found it: no commit, no dirty file. Note
  where this sits — the local backend distrusts its own model, rather than
  Workbench changing what `finished` means for every backend. That question is
  still open below.

The last two are the same shape as the SDK-level distrust the Claude adapter
already has (`stopped_early` invalidating a self-reported outcome), which is
reassuring: a self-reported outcome is worth exactly as much as the evidence
beside it.

**And the model itself is a decision with evidence, not a benchmark.** Three
were measured on the node, on the same small task, through
`scripts/test_local_model.py`:

| model | weights | result |
|---|---|---|
| `qwen2.5-coder:7b` | 4.7 GB | never used the tool channel; wrote every call as prose |
| `qwen3:8b` | 5.2 GB | completed it, 112s over 8 turns |
| `gpt-oss:20b` | 13 GB | completed it, 53s over 10 turns |

The one that looks best on paper is the one that cannot do it at all. The
fastest is a mixture of experts that activates a fraction of itself per token,
so it beats a model a quarter its size on a card that cannot hold either
comfortably. Neither fact is visible from a model card, which is the argument
for the harness existing: one small task against a real endpoint, with every
check reading the worktree rather than the model's own summary, because those
two disagree more often than seems possible.

`qwen3:8b` is the default for fitting rather than for winning — 13 GB of
weights is a bet on a machine nobody has described yet, and a node with the
memory can say so through `WORKBENCH_LOCAL_MODEL`.

One consequence reached back into the vendor-neutral half. `prompts.execute_prompt` used
to tell the agent to use the `workbench-outcome` skill, which is one backend's mechanism
sitting in the module that exists to have none. It now states the *obligation* — report
finished, failed, or needs re-planning — and each backend appends the sentence saying how:
a skill for Claude, a `report_outcome` tool for the local loop. Both reach the same
`POST /api/runs/{id}/outcome`, so nothing above the seam learns there were two ways.

The rule is enforced rather than documented: `agents/tests/test_seam.py` parses every
module in the package and fails if a vendor SDK is imported anywhere else. That matters
because of how this decays — not by someone rejecting the decision, but by a series of
individually reasonable imports of the SDK's own types from the runner or a template
helper, after which the seam is gone and nobody notices. The registry imports backend
modules lazily inside the factory for the same reason the test exists: the web process
resolves backend names constantly and must never pull an SDK into its import graph to do
it.

**Runs bill a subscription, not the metered API.** Chosen deliberately, and the
default in `config.py` rather than a convention someone has to remember.

The thing being defended against is silence. Both credentials are read from the process
environment, so an `ANTHROPIC_API_KEY` arriving for any reason — exported in a shell,
inherited from a parent, added to `/etc/workbench/env` for an unrelated tool — would move
every run onto per-token billing with nothing visible in Workbench changing. The first
sign would be an invoice. So the runner *strips* those variables rather than merely
declining to set them, and `WORKBENCH_BILLING=api` is how someone opts in out loud.

Three consequences that shaped code:

- **The credential is a file in the service user's home**, not a secret in
  `/etc/workbench/env`. Which account pays is therefore decided by which user the unit
  runs as — concrete rather than ambient. It also has to be *writable*, because the OAuth
  token is refreshed periodically, which is why the unit's `ReadWritePaths` covers
  `~/.claude`. Get that wrong and runs work for days and then stop.
- **The scarce resource is a rate-limit window, not dollars.** `RateLimitEvent` is
  translated into a structured notice carrying the window type, utilisation, and reset
  time, because "which run exhausted the five-hour window" is the question that will
  actually get asked. Those readings are recovered from `run_events` and shown as a meter
  on every page — every page, because the window belongs to the account rather than to a
  run, and is spent by anything else using the same subscription. The panel renders with
  no readings too: the moment someone wants it is *before* starting a run, so one that
  appeared only after the first run would be useless exactly when it was needed. It also
  raises the stakes on the concurrency cap: three taps starting three agents wastes a
  window rather than a few dollars.
- **`runs.total_cost_usd` is not a bill.** Under subscription auth it is the backend's
  own token valuation. Useful for spotting a runaway run, misleading if read as money.

None of this is Claude-specific by construction: `API_CREDENTIAL_VARS` is a list in
`config.py`, and the next backend adds its own spelling to it.

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

**The agent commits; Workbench pushes and opens the pull request.** Split deliberately,
and the prompt says so out loud, so that a run which dies midway leaves its work on a
branch rather than a half-formed pull request. Two consequences that are easy to get
backwards. The two halves use *different credentials* — the push rides the service
account's SSH deploy key, opening the pull request needs `WORKBENCH_GITHUB_TOKEN`
through the API — so a machine with one and not the other pushes successfully and then
stops, which is a notice on the run rather than a failure of it. And the pull request
targets **whatever the worktree was cut from**, not the repository's default branch:
that is what sends work into `staging` on a project that promotes through it, and what
makes a task branched from another task stack onto it instead of jumping the queue.

For the same reason the origin picker defaults to `staging`. A branch cut from `main`
and aimed at `staging` carries every promotion merge `main` has had since, and the pull
request lists them as if they were part of the change. Note this is the default a person
sees, not the fallback for an origin nobody ever chose — that stays on the default branch,
because a project reached through the JSON API may have nothing called `staging` at all.

Publishing happens only on the path that closes the task: an explicit `finished` outcome
that was not cut short. A run that says nothing, or that ran out of turns, leaves its
commits for a person. A run with no commits at all is a notice and not a pull request —
see the open question about whether that should be an outcome of its own.

**A run ends when the agent is done; talking to it afterwards is a separate act.**
Plan and execute runs stop the moment the agent delivers its result. They used to wait
`input_idle_seconds` — five minutes — in case somebody typed, which bought nothing and
cost a lot: the run stayed `running` long after it was finished, held one of two
concurrency slots, and delayed the pull request by the length of the window. It could not
have worked either, because `request.inputs` is only pulled *between* turns, so nothing
typed during a plan run ever reached the agent.

A conversation still waits, because that is the whole point of one. To talk to a finished
run, continue it: a new `conversation`-phase run scoped to the same task, resuming the
same session, in the same worktree — which it must be, because a backend's session token
is keyed to the directory it was issued in. A new run rather than a resurrection of the
old one, so the record of what happened stays what happened. So a dialog is something a
person chooses, not something every run waits around on the chance of.

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
  `EnvironmentFile` — **by every unit that spends one, which for a while meant the wrong
  unit**. The deployer read that file and the run unit did not, so `WORKBENCH_GITHUB_TOKEN`
  reached the process that reports staging acceptance and not the process that opens pull
  requests. A correctly configured machine pushed every branch and then reported the token
  as unset. Nothing looked broken, because publishing is deliberately not allowed to fail a
  run: the symptom was pull requests that never arrived on runs that said they succeeded.
  Granting it is one line, and it is `EnvironmentFile=-` rather than `EnvironmentFile=` —
  a fresh install has no such file, and a unit that insisted on one would turn a missing
  pull request into a missing run. Alternatively authenticate the bundled CLI once as that user to
  bill against a Claude subscription instead of the API — which is what this install
  does: `sudo -iu workbench <venv>/bin/python -m workbench.doctor --login`. Either way
  the credential is readable by model-authored shell commands running as that user —
  inherent, but worth stating.
- **The credential is two paths, not one.** `~/.claude` is a directory; `~/.claude.json`
  is a separate file beside it. An allowlist naming only the first leaves the second
  read-only under `ProtectSystem=strict`, and the CLI writes both. That failure arrives
  late — reads work, so runs succeed until the OAuth token is refreshed and cannot be
  saved. Both units grant both paths, and the run unit also grants `~/.ssh`, which `ssh`
  writes on first connection.
- **The subscription login expires on a clock nothing was watching.** The credential is
  two dates, not one: an eight-hour access token the backend renews unattended, and a
  renewal window of about a fortnight that is anchored to the original browser login and
  is *not* extended by renewing. So a server signed in once stops working roughly two
  weeks later however much it runs, and `claude auth status` keeps reporting a healthy
  `claude.ai` login throughout — it answers which account, never whether it works. The
  doctor now reads the window itself and warns three days out. Automating past that is
  not possible: the SDK has no auth surface, and the only unattended renewal that exists
  is the one a run already does for free. See `docs/learning-notes.md`.
- **The deploy key can only be used by a remote that speaks SSH, and clones do not.**
  Every clone is made from `projects.github_url`, which `RepoRef.url` always rebuilds as
  HTTPS whatever was typed — so `origin` is HTTPS and a push asks for a password GitHub
  stopped accepting. Nothing caught it because fetching a public repository needs no
  credentials at all: push is the first operation in a run that authenticates, and the
  doctor's key check passed the whole time it was broken. `push_branch` now sets
  `remote.origin.pushurl` to the SSH form on the way in, leaving the fetch URL alone —
  split rather than switched, because requiring a key to *read* would mean a key just to
  add a public project. Doing it at push time rather than clone time is what repairs the
  clones that already exist, since nothing re-clones them.
- **The doctor knows about the pull request token, because nothing else did.** Whether
  `WORKBENCH_GITHUB_TOKEN` is installed is checked without a network, so it reaches the
  page banner too; whether GitHub still accepts it, and when it expires, needs one and so
  does not. The check reads `/etc/workbench/env` rather than its own environment — the
  token lives in a *unit's* environment, and a person running the doctor by hand has no
  such thing, so reading `os.environ` alone would report it missing on a machine where it
  is configured perfectly. A file it cannot read is `unknown`, never `fail`: mode 0600
  owned by the service account is the correct state, and a person running as themselves
  must not be told their token is gone. The expiry warning exists for the same reason the
  agent credential's does — a fine-grained PAT lasts 90 days by default, and the failure
  when it lapses is pull requests quietly not appearing on runs that report success.
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

**Every step converges rather than firing on a change.** Installing units, restarting the
service, and running staging acceptance all decide from state — is the rendered unit
different, is the running process older than the checkout, has this revision been reported
on — not from "did this tick pull something". All three were the other way once, and all
three broke identically: a deploy that changed the checkout and then died left work no
later tick would ever pick up, with nothing on the machine looking wrong. The third was
the worst, because it left production serving code five commits behind what the repository
said was deployed. `docs/learning-notes.md` has the post-mortems.

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

**The `staging` → `main` pull request now opens itself**, in
`.github/workflows/promote.yml`, triggered by GitHub Actions' `on: status` event — which
in this repository only ever fires for `staging-acceptance`, since that is the only thing
that posts a classic commit status here; CI's own results are check runs, a different
mechanism, and never trigger it. The workflow opens the PR (or leaves an already-open one
alone) once that status goes green, using the run's own scoped `GITHUB_TOKEN` rather than
`WORKBENCH_GITHUB_TOKEN` — opening a pull request needs `pull_requests: write`, and the
server's token is readable by any agent running there, so this belongs in Actions and not
on the box. One thing worth knowing before touching that file: `on: status` workflows run
only from the *default branch's* copy, so a change to it does nothing until it reaches
`main`, whatever branch the status itself was posted for.

**`main` refuses anything that is not from `staging`**, via
`.github/workflows/guard-main.yml`, a required check named
`Only staging may merge into main` that fails immediately, with a legible reason, when a
pull request into `main` has any other head branch. Before this existed such a PR didn't
fail — it sat forever waiting on `staging-acceptance`, a status that is only ever posted
for `staging`'s own commits, so the actual problem never surfaced as anything but
silence. Ruleset required-check syntax cannot express "only from branch X" directly,
which is why this needed its own job rather than a ruleset setting.

This is also what lets *Require branches to be up to date before merging* stay off.
That setting exists to stop a PR merging against a base it was never actually tested
against — which matters when multiple PRs can race to change the same base branch. Once
the guard check is required, exactly one branch can ever reach `main` through a pull
request, and nothing else lands there without an admin's deliberate bypass, so there is
no second PR to race. Turning the setting on would instead reintroduce a version of the
`staging-acceptance` chicken-and-egg: GitHub's "Update branch" button makes a synthetic
merge commit that is never itself deployed as `staging` and so can never earn a
`staging-acceptance` status — permanently blocking the very merge it was meant to
unblock, the moment `main` and `staging` drift by even one commit (an admin bypass
hotfix, say). Leaving it off avoids that trap without giving up anything the guard check
doesn't already cover.

**Merging is deliberately a human action.** The status goes green and the pull request
opens on its own; nothing merges itself. This tool's purpose is running agents that write
code, and agent-authored
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

**The move to `/srv` bends it the same way, and deliberately.** The deployer re-renders
units but never relocates, so this code can reach a server and change nothing until
somebody runs `install.sh` by hand. Relocating a live deployment underneath a running
service is not a decision a five-minute timer should be making unattended at 3am. The
cutover is in `docs/deployment-setup.md`; it is reversible, because the relocation copies
rather than moves.

**The deployer runs as root, the app does not, and the installer now works the same way.**
Both need root — one to restart the unit, the other to create an account and write to
/etc — and both drop to the checkout's owner for every command touching git, the
virtualenv, the database, or that account's home. Doing any of it as root leaves
root-owned files the unprivileged service can no longer write, which surfaces later as a
service that starts and then cannot save anything.

That drop lives in `install.service_run` and `deploy._run` delegates to it. One copy on
purpose: two spellings of "become the service account" is two places for it to drift, and
the drifting one would be creating root-owned files in a directory the service must
write. It uses `subprocess`'s own `user=` rather than `runuser`, because runuser opens a
PAM session and PAM logs every one — three lines per command into the journal that is the
only place a bad deploy explains itself.

For the same reason `install.service_user()` reads the *checkout's owner* rather than the
current euid — reading the euid would silently re-render every unit with `User=root` on
the first automatic deploy, handing an agent a root shell. Now that the checkout is owned
by the service account, that indirection is load-bearing rather than incidental.

**`install.py` imports nothing but the standard library**, and that is a constraint rather
than an aesthetic. It runs *before* a virtualenv exists, because one of the first things
it decides is whether this checkout is where the deployment belongs — and building a few
hundred megabytes of environment in a directory it is about to abandon is the one cost
worth restructuring to avoid. `install.sh` therefore starts it with `uv run --no-project`
and a `PYTHONPATH`, and the environment is built afterwards, at the deployment, by the
account that will own it.

**Relocation copies rather than moves.** The checkout someone cloned is left exactly as it
was, so the operation needs no confirmation, breaks nothing if it fails, and leaves the
old database as a free point-in-time backup; rolling back is re-running the old
installer with `WORKBENCH_DEPLOYMENT_ROOT` pointing at itself. A deployment already there
and already owned by the account is handed off to rather than copied over, which is what
makes re-running the *abandoned* checkout's `install.sh` harmless — and somebody will,
because that is the directory they remember.

### Known trap: self-deployment

Workbench is intended to eventually be developed inside Workbench, and a task ending in
"restart the workbench service" would kill the process serving the request.

**Half of this is now handled.** Deploys run from their own systemd unit, so the restart
they trigger lands on a different cgroup and cannot kill the deployer — and since the
timer owns deployment, nothing has to reach back into the app to trigger one at all.

The web tier is now a pure reader, and the runner is a separate process reached only by
a run id: `python -m workbench.runs.runner <id>`. Every event is committed as it happens,
so a run that dies halfway is still legible, and the runner catches SIGTERM to record
`cancelled` rather than vanishing.

**But `start_new_session=True` is not enough on its own, and this changes what the runs
slice has to do.** A new session detaches a process from its process group; it does not
move it out of the unit's control group. systemd's default `KillMode=control-group` kills
*all remaining processes in the cgroup* on unit stop, so a runner spawned by the app is
still killed by a deploy restarting that app — verified in `systemd.kill(5)` rather than
assumed. Deploys land every five minutes with nobody watching, so this would be routine,
not rare.

**Settled: one systemd unit per run.** `workbench-run@<id>.service`, started over D-Bus by
the app and therefore in its own control group, so a deploy restarting the web service
cannot reach it. Chosen over a transient user scope not for the deploy problem — either
would do — but because a unit is a *job*: addressable, inspectable, stoppable, and
resource-limited, which is the shape this grows into when work starts being dispatched to
other machines.

Two things that are easy to get backwards. A unit is not remote dispatch; systemd is an
init system, not a scheduler, and what makes another machine cheap is `runs.executor` plus
an opaque `runs.handle` — a third `Executor` implementation, not a different init system.
And unit-per-run does not *require* polkit: a named unit in the user manager needs no
privilege, but only gets the controllers that manager was delegated (`memory pids` here —
no `cpu`, no `io`, and no device policy), which rules it out for the heavy jobs this is
for.

**`docs/running-agents.md` is the long version** — what a run consists of, why a process
group is not a control group, and what each option costs. Worth reading before that file,
because the runner looks over-built until the cgroup behaviour is clear.

## Open questions

Unresolved. Recorded here so they are not rediscovered later.

- **Agent sessions are directory-scoped — for one backend.** Claude's resume token is
  keyed to the directory it ran in, which conflicts with "worktrees are disposable":
  deleting a task's worktree orphans the `resume_token` its runs point at. Per-task
  worktrees narrow this but do not close it. Some SDKs expose a pluggable session store,
  which would let those live in our own SQLite instead of on disk. Worth noting that the
  local backend has no such problem and not because it solved one — it had to keep the
  transcript itself, since a `/chat/completions` endpoint remembers nothing, so the
  conversation is a file under `data/sessions/` that no worktree owns. That is what the
  fix for Claude would look like if an SDK ever allows it.
- **Polling is how the stream tails.** There is no in-process signal available: the runner
  is a different process in a different cgroup, and SQLite has no LISTEN/NOTIFY, so the
  table is the only thing the two share. Once per second per open page is fine at this
  scale and would not be on a busy one.
- **`setup_command` is per project, but the need is per worktree.** A project whose setup
  is "symlink `.env` and the venv from the main checkout" cannot express that as one
  command without knowing the source path.
- **How much does the dedicated account actually bound?** It now exists — the service
  runs as `workbench`, which owns `/srv/workbench` and nothing else and has no sudo — so
  the blast radius is bounded to files recoverable from GitHub, plus the credential in
  that account's home, which model-authored shell commands can still read. That last part
  is inherent to running an agent on a subscription and is not fixed by any account
  boundary. What is genuinely untested is whether the bound holds in practice, because no
  agent has yet run on the real server.
- **Does the agent work under `ProtectSystem=strict`?** The credential paths are now
  granted explicitly (`~/.claude`, `~/.claude.json`, `~/.ssh`), but every one of those was
  reasoned about rather than observed — no token has been refreshed on this machine. The
  doctor reports `unknown` rather than crying wolf if the probe cannot run, and the fix
  for anything missed is one more `ReadWritePaths` line.
- **Is `tailscale serve status` readable by a non-operator account?** The banner's check
  runs as the service account, which is not the tailnet operator. If it turns out to need
  that, the check degrades to `unknown` and the fix is
  `sudo tailscale set --operator=workbench`.
- **Is "no commits" a failure?** A run where the agent correctly concludes nothing needs
  changing produced no pull request, but calling that `failed` reads as a malfunction when
  it was judgement. Probably wants a third outcome.

  The local backend has since taken a position on half of it, and deliberately in the
  narrower place: it refuses a self-reported `finished` while the worktree is exactly as
  the run found it, because a small model claims to have finished things it has not
  started. That is a backend distrusting its own model, not Workbench deciding what
  `finished` means — which is still this question, still open, and now with evidence that
  the two cases ("nothing needed doing" and "nothing was done") are told apart by asking
  the agent rather than by counting commits.
- **Rate-limit readings are only as fresh as the last run.** The panel updates when a
  backend reports a reading, and nothing else asks. A one-turn probe session does emit
  one — measured, it works — but it costs about 11,600 cache-creation tokens a shot,
  because the CLI rebuilds its system prompt and tool definitions every fresh session.
  That rules out a timer: probing every fifteen minutes would spend a substantial slice
  of a five-hour window measuring that window. Wants on-demand refresh past a staleness
  threshold, default off, and a `rate_limit_readings` table — a probe has no run to hang
  events on, and that table would also retire the `json_extract` scan. Note that
  `utilization` was null in both real samples, so the percentage bar is the optional
  extra and the status is the primary signal.

- **Event log growth is unbounded.** Every tool call of every run is a row, kept forever.
  Fine now; wants pruning before it is not.
- **Whether a parent task's status should derive from its children.** Currently
  independent, with a `2/5` progress count shown instead.
- **Should the server hold a long-lived token instead?** `claude setup-token` mints one
  (inference-only scope, supplied as `CLAUDE_CODE_OAUTH_TOKEN`), which is the shape a
  headless box wants and would retire the fortnightly re-login entirely. It bills the
  subscription, so it is untouched by `API_CREDENTIAL_VARS` stripping, and it would ride
  the `EnvironmentFile` mechanism `/etc/workbench/env` already has. Against it: that
  moves the credential out of the service account's home, which is currently what makes
  "which account pays" concrete rather than ambient.
- **No deploy has ever failed on the real server.** Every refusal path — dirty checkout,
  crash-on-boot preflight, migration failure — is proven on CI runners and in unit tests,
  never on this machine. The first genuine bad deploy is still an unknown. A *run* has now
  failed there, at authentication, which is what the credential-window check above came
  from.

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
- **SSE replay.** Done, and it is the reason `run_events` is numbered per run rather than
  globally. A reader says how far it got — through `Last-Event-ID` on reconnect, or `after`
  on first load — and gets the rest exactly once. A phone that sleeps through half a run
  loses nothing, and reading a run back a week later is the same query with a different
  number in it.
- **Nothing caps concurrency.** `max_concurrent_runs` defaults to 2, and `start` reaps
  before checking it — otherwise a run killed mid-flight holds a slot forever and the cap
  becomes a way to lock yourself out.
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
