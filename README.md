# workbench

[![CI](https://github.com/idm23/workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/idm23/workbench/actions/workflows/ci.yml)

A personal tool for managing software projects on a home server — projects, a todo tree per
project, and tasks worked either by hand or by a Claude agent, with a written summary either way.

**Status:** early. Users own projects that point at GitHub repositories, and each project has a
tree of tasks you can add, complete, and delete from a phone. A project can be cloned to the server,
and each task can be given its own git worktree.

Agents exist: `workbench/agents/` drives Claude — or a model served on your own GPU — behind a
vendor-neutral seam, a run is carried out by its own systemd unit, and a run's page streams output
live and replays whatever a sleeping phone missed. The service runs as a dedicated unprivileged
account that owns nothing but its own deployment under `/srv`.

**No agent has yet completed a run on the real server.** Signing one in needs a browser login that
no installer can perform, so `./install.sh` finishes by saying so with the exact command, and
`python -m workbench.doctor` re-checks it — along with the deploy key and the tailnet — any time
afterwards. See `CLAUDE.md` for where it is going.

## Quick start

On a fresh Ubuntu machine:

```sh
git clone https://github.com/idm23/workbench.git
cd workbench
./install.sh
```

That is the whole install. It fetches `uv` if you do not have it, creates a dedicated
unprivileged `workbench` account, moves the deployment to `/srv/workbench` and gives it to that
account, builds a virtualenv with the exact locked dependency versions, applies migrations,
installs a systemd service, and waits until the app answers on <http://127.0.0.1:8787>. It asks
for `sudo` once, up front.

**Where it ends up is not where you cloned it.** The service runs as its own account, and Ubuntu
creates home directories mode 0750 — so a checkout under your home is one that account cannot even
traverse. The installer therefore *copies* the checkout to `/srv/workbench`, leaving yours exactly
as it was, and continues from there. Re-running `./install.sh` from either directory afterwards is
safe: the one you cloned hands off to the deployment rather than copying over it.

It finishes by printing whatever still needs a person — signing the agent in, adding a deploy key,
publishing over Tailscale. `python -m workbench.doctor` re-checks all of it at any time.

`install.sh` itself is a short shell bootstrap whose only job is getting `uv` onto the machine —
the actual work lives in `src/workbench/install.py`, which imports `workbench.config` so the port
and database path cannot drift from what the running app uses.

Re-running it is safe — every step checks before acting, and your data is untouched.

> **This is a promise the repo keeps under test.** `scripts/test_fresh_install.py` provisions a
> clean Ubuntu container, runs the install, exercises the app over HTTP, and re-runs the install
> to prove it is repeatable. If a setup step ever becomes necessary that is not in `install.sh`,
> that test fails.

## What is in here

| | |
|---|---|
| `install.sh` | The only entry point — a ~12-line bootstrap that installs `uv` and hands off. |
| `src/workbench/install.py` | The installer proper. Python, so it shares config with the app. |
| `src/workbench/app.py` | The web application: routes and templates. |
| `src/workbench/agents/` | The backends behind one seam: `claude.py`, and `local.py` for your own GPU. |
| `src/workbench/database/` | `models.py` (the schema) and `db.py` (engine and sessions). |
| `src/workbench/git/` | `github.py`, `worktrees.py`, `revision.py` — everything that shells to git. |
| `src/workbench/tasks/` | `tree.py` (the shape a page renders) and `store.py` (every write). |
| `src/workbench/api.py` | The JSON routes, over the same operations as the forms. |
| `src/workbench/deploy.py` | Pulls, migrates, and restarts. Run on a timer; see below. |
| `src/workbench/doctor.py` | What still needs a person, and whether they have done it. |
| `src/workbench/config.py` | Everything read from the environment, with repo-relative defaults. |
| `alembic/` | Migrations. Applied by the installer and by every deploy. |
| `scripts/` | The end-to-end tests and the staging acceptance run. |
| `deploy/*.template` | The systemd units, rendered with detected paths. |
| `CLAUDE.md` | Design doc, decisions, and open questions. |
| `docs/deployment-setup.md` | Turning the pipeline on, once. |
| `docs/server-conventions.md` | How the home server launches things, and why. |
| `docs/learning-notes.md` | What building this taught me about systemd, git, and SQLite. |

**Tests live beside the code they cover** — `src/workbench/database/tests/`,
`src/workbench/git/tests/`. Only the ones spanning several modules at once stay in the top-level
`tests/`, which is currently the deployer and the systemd units. They are excluded from the built
wheel, so nothing test-related is installed onto the server.

## The API

Tasks can be created by something other than a person with a phone — a script, a terminal, or an
agent. The JSON routes live under `/api` and perform the **same writes** as the HTML forms, through
`workbench.tasks.store`, so the two cannot drift.

```sh
curl -s https://homebox-core.tail4c4cf3.ts.net/api/projects

curl -s -X POST https://homebox-core.tail4c4cf3.ts.net/api/projects/1/tasks \
  -H 'content-type: application/json' \
  -d '{"title": "Make the clone directory configurable", "parent_id": 3}'

curl -s -X PATCH https://homebox-core.tail4c4cf3.ts.net/api/tasks/7 \
  -H 'content-type: application/json' -d '{"status": "done"}'
```

| | |
|---|---|
| `GET /api/projects` | id, owner, repo, whether it is cloned, open task count |
| `GET /api/projects/{id}/tasks` | the tree, nested |
| `POST /api/projects/{id}/tasks` | title, optional body and `parent_id` |
| `PATCH /api/tasks/{id}` | any of title, body, status |
| `DELETE /api/tasks/{id}` | the task and everything under it |

Browsable at **`/docs`**, which FastAPI generates from the route signatures.

**There is no authentication, and that is not an oversight.** These routes are exactly as exposed
as the forms they mirror — both reachable by anything that can reach the app, which is a two-device
personal tailnet. Adding a token to the JSON half while leaving the HTML half open would be
theatre. If the app gains auth it belongs in front of both.

## Tests

```sh
uv run pytest                              # schema, migrations against real rows, units, deployer
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run scripts/smoke_test.py               # end-to-end against a running install
uv run scripts/test_fresh_install.py       # clean Ubuntu container, install, verify
uv run scripts/test_deploy_cycle.py --force  # the deploy loop, on real systemd
```

CI runs all of these on every push and pull request, plus `shellcheck install.sh` and
`alembic check` — the latter fails if a model has been changed without generating a migration,
which is invisible locally because your own database already has the change applied.

`src/workbench/database/tests/test_migrations.py` applies migrations to a database that
already has rows in it. Every
other migration check runs against empty tables, where the failures that actually happen — a
`NOT NULL` column with no default, a unique constraint real data violates — cannot occur.

`test_deploy_cycle.py` is the only test that exercises systemd. It installs real units, needs
passwordless sudo, and tears them down afterwards, so it refuses to run unless `CI=true` or
`--force` — a laptop is not a disposable VM. Everything it covers (units loading, the timer
scheduling, migrating, restarting, the health check, and the deployer's refusals) is otherwise
unexecuted by any test until it runs on the server for real.

`test_fresh_install.py` uses the working tree by default so you can check uncommitted work. Pass
`--from-github` to test what someone cloning the public repo actually gets.

It needs Docker rather than LXD, because Docker requires no host-side initialisation and is what
CI runners provide. A plain container has no systemd, so `install.sh` detects that and skips the
service step; the systemd path is covered when deploying to the real server.

## Exposing it over Tailscale

Optional, and deliberately not part of `install.sh` — joining a tailnet needs a browser login to
an account a script cannot know about.

```sh
sudo tailscale set --operator=$USER   # once, if you have not already
tailscale serve --bg 8787
tailscale serve status
```

Live at **https://homebox-core.tail4c4cf3.ts.net** — tailnet only, not reachable from the public
internet. Never use `tailscale funnel`, which would publish it.

Valid HTTPS requires **HTTPS Certificates** enabled in the Tailscale admin console
(DNS → HTTPS Certificates). Check with `tailscale status --json | grep CertDomains`; an empty
value means it is off and the phone will get a certificate warning.

## Deploying

**Merging to `main` is the deployment.** A systemd timer checks for new commits every five
minutes; when it finds some it fast-forwards the checkout, syncs dependencies, applies migrations,
reinstalls the units if their templates changed, and restarts the service. Nothing to run by hand,
and no schema step to remember.

```sh
systemctl list-timers workbench-deploy.timer      # when the next check lands
sudo systemctl start workbench-deploy             # deploy right now, do not wait
journalctl -u workbench-deploy -n 50 --no-pager   # what the last one did
sudo systemctl disable --now workbench-deploy.timer   # stop deploying automatically
```

### Turning it on, once

The deployer cannot install itself: a machine with no timer never checks for the commit that would
give it one. So on a server that predates this, `install.sh` has to be run by hand exactly one
final time.

```sh
ssh <server> 'cd ~/workbench && git pull && ./install.sh'
```

From then on that command is never needed again — and re-running it is harmless if you do.

### This makes CI a gate, not a report

Once the timer is live, a merge to `main` reaches the server within five minutes with nobody
watching. Branch protection requiring the CI checks is what stands between a red build and a
restarted service, so it stops being a nicety at this point.

## Staging

Nothing reaches `main` without having actually run somewhere first.

```
  branch ──PR──▶ staging ──deploys in 5min──▶ :8788 ──acceptance──▶ commit status
                                                                          │
                                              main ◀── you merge ◀────────┘
                                               │
                                               └── deploys in 5min ──▶ :8787
```

**Staging is a second install on the same box**, from the `staging` branch, on port 8788, with its
own systemd units and its own `data/`. It comes almost free: a second checkout already has a
separate database and virtualenv because both are repo-relative, so only the unit names needed
disambiguating.

**Before migrating, it restores a snapshot of production's database.** This is the point of it. A
migration that passes against empty tables routinely fails against real rows — a `NOT NULL` column
with no default, a unique constraint real data violates — and staging is the only place that gets
caught before production. The snapshot uses SQLite's backup API rather than a file copy, because
production is live and in WAL mode.

**Acceptance runs on its own** after each staging deploy, and posts a `staging-acceptance` commit
status to GitHub. That call is *outbound*, which is what makes this work without exposing the
server: GitHub cannot ask how staging went, so the server tells it. Branch protection on `main`
requires that status, so a commit that has never run on staging cannot be promoted.

Promotion itself is a click. The status goes green on its own; merging is yours.

```sh
sudo systemctl start workbench-staging-deploy          # deploy staging now
journalctl -u workbench-staging-deploy -n 50 --no-pager
uv run scripts/staging_acceptance.py --no-report       # run the checks without reporting
```

### Reaching staging from a phone

Production is published on 443; staging goes on a second port:

```sh
tailscale serve --bg --https=8443 8788
tailscale serve status
```

Staging is then at **https://homebox-core.tail4c4cf3.ts.net:8443**, and the port makes it obvious
which instance is on screen. Adding it leaves the production config alone, and the certificate
covers the hostname regardless of port.

**Do not serve staging on a path** (`--set-path=/staging`). Tailscale strips the prefix before
proxying, so the app sees `/users/1` and renders its links root-relative — the first click would
land on production while looking like staging. Every template here uses absolute paths, so this
would be silent and wrong rather than broken and obvious.

**Anything you create on staging is temporary.** Every deploy restores a fresh snapshot of
production's database before migrating, so tasks added while clicking around disappear on the next
merge to `staging`. That is the point — migrations are tested against real rows rather than
whatever the last person left behind — but it is worth knowing before wondering where something
went.

### Setting staging up, once

```sh
git clone https://github.com/idm23/workbench.git ~/workbench-staging
cd ~/workbench-staging && git checkout staging
WORKBENCH_INSTANCE=staging WORKBENCH_PORT=8788 WORKBENCH_DEPLOY_BRANCH=staging \
  WORKBENCH_RESTORE_FROM=$HOME/workbench/data/workbench.db ./install.sh
```

Those variables are baked into the rendered units, so they persist across deploys and never need
setting again.

### What GitHub needs

- A `staging` branch.
- **Protect `main`:** require a pull request; require these checks, with branches up to date —
  `Lint, types, and tests`, `Fresh install on clean Ubuntu`, `Deploy cycle on systemd`, and
  `staging-acceptance`. Block force pushes.
- **Protect `staging`:** require the three CI checks, but *not* `staging-acceptance` — that status
  is produced by deploying staging, so requiring it there is circular.
- A fine-grained PAT in `/etc/workbench/env` as `WORKBENCH_GITHUB_TOKEN` (mode 0600), scoped to
  this repository with **Contents: Read and write**, **Pull requests: Read and write**, and
  **Commit statuses: Read and write**. The last is what reports staging acceptance; the first two
  are what let a run open its pull request. `python -m workbench.doctor` says whether it is
  installed, whether GitHub still accepts it, and when it expires.

Requiring `staging-acceptance` on `main` is what enforces the flow, and it means a hotfix cannot
skip staging without an admin override. That is deliberate, but worth knowing before you need it at
2am.

**[`docs/deployment-setup.md`](docs/deployment-setup.md) is the click-level runbook** — the order
these have to happen in, the exact ruleset fields, and the two chicken-and-egg problems that decide
that order.

It **polls** rather than being pushed to, because the server has no public ingress — it is on a
tailnet and `tailscale funnel` is ruled out, so neither GitHub nor a CI runner can reach in. A
self-hosted runner would work but means parking a long-lived credential here and letting workflow
code execute on the box. The cost of polling is that a merge lands within five minutes rather than
instantly.

It is also deliberately timid. A checkout that is dirty, on another branch, or carrying a local
commit is reported into the journal and left completely alone, so working on the server by hand is
never interrupted and nothing is discarded.

Uncommitted changes are checked for **explicitly**, rather than left to `git merge --ff-only`. That
distinction is load-bearing: git only refuses a fast-forward that would overwrite a file you edited,
so a commit touching anything else merges straight over your working state. Whether your work was
respected would depend on what the incoming commit happened to contain. Untracked files are allowed
through — a stray log is not work in progress, and git still refuses on its own if a commit would
overwrite one.

If migrations fail it stops **before** restarting, leaving the old code running against the schema
it was built for rather than turning a failed deploy into an outage.

The deployer runs as a separate unit from the app rather than as something the app does to itself.
Restarting a service from inside that service kills the process doing the restarting; from its own
unit the restart lands on a different cgroup. `journalctl -u workbench-deploy` is where a deploy
that went wrong explains itself.

## Troubleshooting

**`tailscale serve` hangs and never returns.** The CLI user is not the Tailscale operator —
`tailscale debug prefs` shows `"OperatorUser": null`. State-changing commands then need root, and
over a non-interactive SSH session there is no TTY to prompt on, so the command blocks instead of
erroring. The hung process ignores `SIGTERM`, so `timeout` will not clear it; use
`pkill -9 -x tailscale`, which matches the CLI only and leaves the `tailscaled` daemon alone. Fix
with `sudo tailscale set --operator=$USER`.

**`401 Unauthorized: must be root ... to serve a path or Unix socket`.** Being the operator is
enough to proxy a *port*, which is what this uses now, but serving a *filesystem path* needs root
regardless. If you see this, you are running an older `serve` config pointed at a directory —
`tailscale serve reset`, then re-run the port form above.

**`docker is installed but not usable`.** Usually a selected-but-not-running Docker Desktop
context while the system daemon is up. `docker context ls` will show it; either prefix commands
with `DOCKER_CONTEXT=default` or run `docker context use default`.

**The service will not start.** `journalctl -u workbench -n 50 --no-pager`. The unit runs under
`ProtectSystem=strict` with only `data/` writable, so anything trying to write elsewhere in the
repo will fail here but work when run by hand.

## Development

The laptop and the server are on different tailnets, so the laptop reaches the server over the
LAN (`ian@192.168.1.199`) and tailnet behaviour can only be checked from the phone.

The laptop runs Python 3.10 and the server 3.14, but `uv` installs its own interpreter pinned by
`.python-version`, so both run the same version regardless of what the OS ships.
