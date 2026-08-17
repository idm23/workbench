# workbench

[![CI](https://github.com/idm23/workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/idm23/workbench/actions/workflows/ci.yml)

A personal tool for managing software projects on a home server — projects, a todo tree per
project, and tasks worked either by hand or by a Claude agent, with a written summary either way.

**Status:** early. Users own projects that point at GitHub repositories, each project holds a tree
of tasks, and a leaf task can be handed to a Claude agent: it plans, you review the plan on your
phone, and on approval it does the work and opens a pull request. See `CLAUDE.md` for where it is
going next.

## Quick start

On a fresh Ubuntu machine:

```sh
git clone https://github.com/idm23/workbench.git
cd workbench
./install.sh
```

That is the whole install. It fetches `uv` if you do not have it, creates a virtualenv with the
exact locked dependency versions, applies migrations, installs a systemd service, and waits until
the app answers on <http://127.0.0.1:8787>. It asks for `sudo` once, for the service.

Expect it to take a few minutes the first time: `claude-agent-sdk` bundles a ~310 MB native
`claude` binary. That is also what means the server needs no Node and no separately installed CLI.

## Running tasks with an agent

Browsing and managing tasks works immediately. Handing one to Claude needs two credentials that
cannot go in a script — `install.sh` prints both when it finishes:

```sh
claude                                   # sign in, as the user the service runs as

sudo install -d -m 755 /etc/workbench    # then a fine-grained GitHub PAT:
sudo touch /etc/workbench/env && sudo chmod 600 /etc/workbench/env
#   WORKBENCH_GITHUB_TOKEN=github_pat_...   contents:write, pull_requests:write
sudo systemctl restart workbench
```

Then, on a project page: **Clone repository**, add a task, and press **Plan with Claude** on a leaf
task. The agent gets its own branch and git worktree, investigates in plan mode without touching
anything, and stops with a plan. Approve it and the same session resumes to do the work, commit,
push, and open a pull request.

Without the GitHub token, planning and execution still work; only pushing and the pull request do
not. Without the Claude login, starting a run fails with an authentication error.

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
| `src/workbench/` | The application: models, GitHub lookup, routes, templates. |
| `src/workbench/runner.py` | Executes one run. Detached, so a restart cannot orphan it. |
| `src/workbench/agent.py` | The Claude SDK wrapper: plan phase, execute phase. |
| `src/workbench/worktrees.py` | Cloning, and a git worktree per task. |
| `alembic/` | Migrations. Applied by the installer; no manual step. |
| `scripts/smoke_test.py` | Checks a running install actually works, over HTTP. |
| `scripts/test_fresh_install.py` | The full clean-machine install test, in a container. |
| `deploy/workbench.service.template` | The systemd unit, rendered with detected paths. |
| `CLAUDE.md` | Design doc, decisions, and open questions. |
| `docs/server-conventions.md` | How the home server launches things, and why. |

## Tests

```sh
uv run pytest                              # schema, reference parsing, task tree, worktrees
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run scripts/smoke_test.py               # end-to-end against a running install
uv run scripts/test_fresh_install.py       # clean Ubuntu container, install, verify
```

The smoke test stops short of starting an agent run, deliberately: a clean machine has no
credentials, and what it checks instead is that the refusal is a readable message rather than a
traceback.

CI runs all of these on every push and pull request, plus `shellcheck install.sh` and
`alembic check` — the latter fails if a model has been changed without generating a migration,
which is invisible locally because your own database already has the change applied.

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

## Deploying to the home server

```sh
ssh ian@192.168.1.199 'cd ~/workbench && git pull && ./install.sh'
```

`install.sh` restarts the service, so this both updates the code and applies any new migrations.

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
