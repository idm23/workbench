# Setting up the deployment pipeline

A one-time runbook. Once it is done, merging a pull request is the whole deployment and none
of this is needed again. `README.md` describes how the pipeline *works*; this is the
click-level sequence for turning it on.

## The order matters

Two chicken-and-egg problems decide the sequence, and both are easier to avoid than to
untangle:

1. **The deployer cannot install its own timer.** A machine with no timer never checks for
   the commit that would give it one, so the first install is by hand.
2. **`staging-acceptance` cannot be required before it has ever been posted.** The code that
   produces it is *in* the pull request you are trying to merge, so protection gets tightened
   in two passes rather than one.

## Use Rulesets, not Branch protection rules

Both exist in the UI and either would work, but **Rulesets let you type an arbitrary status
check name**. Legacy branch protection only offers a search box, and that box lists only
checks GitHub has seen in the last seven days — `staging-acceptance` has never been posted,
so it would be unselectable until after the server is running.

Settings → Rules → Rulesets.

---

## Step 1 — Protect `main`, lightly

Settings → Rules → Rulesets → **New ruleset** → **New branch ruleset**.

| Field | Value |
|---|---|
| Ruleset name | `main` |
| Enforcement status | **Active** |
| Bypass list | Add yourself as **Repository admin** |
| Target branches | **Add target** → **Include default branch** |

Rules to enable:

- **Restrict deletions**
- **Block force pushes**
- **Require a pull request before merging**, with *Required approvals* set to **0**. You are
  the only reviewer; leaving it at 1 means never being able to merge your own work.
- **Require status checks to pass**, with *Require branches to be up to date before merging*
  left **unticked** — see step 3a for why, once the check that makes it safe exists. Then
  **Add checks** and type each of these exactly, pressing `+` after each:
  - `Lint, types, and tests`
  - `Fresh install on clean Ubuntu`
  - `Deploy cycle on systemd`

> These three strings are the `name:` values of the jobs in `.github/workflows/ci.yml`.
> Renaming a job there silently stops the required check from ever matching, and the pull
> request waits forever on something that will never report. Change them together.

Do **not** add `staging-acceptance` yet — see step 5. Do **not** add
`Only staging may merge into main` yet either: `staging` doesn't exist yet, and the
deployment pipeline pull request this step ends by merging is not from `staging` — see
step 3a, right after `staging` exists.

**On the bypass list:** add yourself as admin. Without it, a genuine 2am hotfix means editing
the ruleset under pressure. With it, bypassing is a deliberate act you can see you took.

Create the ruleset, then merge the deployment pipeline pull request.

## Step 2 — Get production running

The one manual install, on the server:

```sh
cd ~/workbench && git pull && ./install.sh
```

This installs three units (`workbench.service`, `workbench-deploy.service`,
`workbench-deploy.timer`), enables the timer, and prints what is left to do. From here,
merges to `main` deploy themselves.

## Step 3 — Create the `staging` branch and its ruleset

```sh
gh api repos/idm23/workbench/git/refs \
  -f ref=refs/heads/staging \
  -f sha="$(gh api repos/idm23/workbench/git/ref/heads/main --jq .object.sha)"
```

Then a second ruleset, the same way as step 1:

| Field | Value |
|---|---|
| Ruleset name | `staging` |
| Target branches | **Add target** → **Include by pattern** → `staging` |

Rules: **Block force pushes**, **Require a pull request before merging** (0 approvals), and
**Require status checks** with the *same three CI checks*.

> **Do not require `staging-acceptance` here.** That status is produced *by* deploying
> staging. Requiring it on staging is circular and deadlocks the branch.

## Step 3a — Require the staging-only guard

Now that `staging` exists, go back to the `main` ruleset (Settings → Rules → Rulesets →
`main`) → Require status checks → **Add checks** → type `Only staging may merge into main`
exactly → `+` → **Save**.

Unlike `staging-acceptance`, this one needs no "prove it's been posted" wait: it's a pure
git-metadata check from `.github/workflows/guard-main.yml` that runs, and reports, on every
pull request into `main` regardless of whether it's required yet — so it's already reporting
by the time you get here.

Leave *Require branches to be up to date before merging* unticked. This check is what makes
that safe: with it required, `staging` is the only branch that can ever reach `main` through
a pull request, so there's no second PR racing to change the base out from under this one —
the thing that setting exists to prevent. Turning it on would instead reintroduce the
`staging-acceptance` chicken-and-egg: GitHub's "Update branch" button makes a synthetic merge
commit that can never itself be deployed as `staging`, so it can never earn a
`staging-acceptance` status either — permanently deadlocking promotion the moment `main` and
`staging` drift by even one commit. See `CLAUDE.md` for the full reasoning.

## Step 4 — Install staging on the server

```sh
git clone https://github.com/idm23/workbench.git ~/workbench-staging
cd ~/workbench-staging
git checkout staging

WORKBENCH_INSTANCE=staging WORKBENCH_PORT=8788 WORKBENCH_DEPLOY_BRANCH=staging \
  WORKBENCH_RESTORE_FROM=$HOME/workbench/data/workbench.db ./install.sh
```

Those four variables are rendered into the units, so they persist across every future deploy
and never need setting again.

**Clone under your home directory, not `/tmp`.** The units set `PrivateTmp=yes`, which gives
the service private `/tmp` and `/var/tmp` namespaces — a checkout under either is invisible
from inside the service, and systemd reports only "the control process exited with error
code" against an empty journal. `install.sh` now refuses this outright rather than letting
you find out the hard way.

Verify both instances:

```sh
systemctl list-timers 'workbench*'          # two timers, each with a next elapse
curl -s localhost:8787/healthz              # production
curl -s localhost:8788/healthz              # staging
```

## Step 5 — Create the token

Settings → Developer settings → Personal access tokens → **Fine-grained tokens** →
**Generate new token**.

| Field | Value |
|---|---|
| Token name | `workbench-server` |
| Expiration | 90 days, or custom — set a reminder, an expired token silently stalls promotion |
| Repository access | **Only select repositories** → `idm23/workbench` |

Repository permissions:

- **Contents** → **Read and write**
- **Pull requests** → **Read and write**
- **Commit statuses** → **Read and write**

Everything else stays *No access*.

Commit statuses is the one staging acceptance needs; the other two are what let a run open its
own pull request. That was deferred once, and this file said to add them "when that lands, not
now" long after it had landed — so a correctly followed runbook produced a token that pushed
branches and opened nothing. `python -m workbench.doctor` now answers this directly rather than
leaving it to a document to stay accurate.

On the server:

```sh
sudo install -d -m 755 /etc/workbench
sudo touch /etc/workbench/env && sudo chmod 600 /etc/workbench/env
sudo tee /etc/workbench/env >/dev/null <<'EOF'
WORKBENCH_GITHUB_TOKEN=github_pat_...
EOF
sudo systemctl restart workbench-staging
```

## Step 6 — Prove the status appears, then require it

Push something trivial to `staging` and wait up to five minutes:

```sh
journalctl -u workbench-staging-deploy -n 50 --no-pager
gh api repos/idm23/workbench/commits/staging/status --jq '.statuses[].context'
```

You are looking for `staging-acceptance`. Once it is there:

Settings → Rules → Rulesets → **`main`** → Require status checks → **Add checks** → type
`staging-acceptance` → `+` → **Save**.

That is the last click. From then on, nothing reaches `main` without having actually run on
staging.

---

## Moving an existing server to `/srv` and a service account

Only for a machine installed before this landed. A fresh clone gets all of it from the
first install and can skip to Step 7.

Nothing moves on its own. The deployer only re-renders units — it never relocates — so
the timer can deliver this code to a server for days and change nothing until somebody
runs `install.sh` by hand. That is the same "one last manual install" carve-out the timer
itself needed, and it is deliberate: relocating a live deployment underneath a running
service is not something a five-minute timer should decide to do at 3am.

**Relocation copies, it never moves.** The old checkout and its database are left exactly
as they were, which is what makes every step below reversible and why there is no backup
step you must not skip. Take one anyway at step 2 — it costs a second.

```sh
# 1. Look before touching anything. A dirty checkout or an in-flight deploy is
#    a reason to stop, not to continue carefully.
cd ~/workbench && git status --porcelain && git rev-parse --short HEAD
systemctl is-active workbench workbench-staging
systemctl list-timers 'workbench*'

# 2. Stop both timers, and DISABLE them — so a reboot part-way through cannot
#    restart one into a half-moved install.
sudo systemctl disable --now workbench-deploy.timer
sudo systemctl disable --now workbench-staging-deploy.timer

# 3. The only irreplaceable thing on the box. Through the backup API, never cp:
#    the database is live and in WAL mode, so a file copy is stale or torn.
cd ~/workbench && .venv/bin/python -c \
  "import sqlite3; a=sqlite3.connect('data/workbench.db'); b=sqlite3.connect('$HOME/workbench-precutover.db'); a.backup(b); b.close(); a.close()"

# 4. Production. This is the one manual install.
cd ~/workbench && git pull && ./install.sh
```

That last command asks for `sudo` once, then: creates the `workbench` account; stops the
service; copies `~/workbench` to `/srv/workbench` (no virtualenv, no worktrees, the
database through the backup API); chowns it; re-execs itself from there; installs `uv`
into the account's home where the deployer looks for it; builds the virtualenv as the
account; migrates; sets the account's git identity and generates its SSH key; re-renders
every unit with `User=workbench` and `WorkingDirectory=/srv/workbench`; rewrites the
polkit rule to grant that account; restarts; health-checks; and prints what is left.

Unit names and the polkit filename do not change, so nothing needs removing — everything
is rewritten in place. The only leftovers are the two old checkouts, and they are your
rollback.

```sh
# 5. Staging — AFTER production, because its WORKBENCH_RESTORE_FROM is baked
#    into its deploy unit and now has to point into /srv.
cd ~/workbench-staging && git pull
WORKBENCH_INSTANCE=staging WORKBENCH_PORT=8788 WORKBENCH_DEPLOY_BRANCH=staging \
  WORKBENCH_RESTORE_FROM=/srv/workbench/data/workbench.db ./install.sh

# 6. Do Step 7 below for both accounts, then re-arm.
sudo systemctl enable --now workbench-deploy.timer
sudo systemctl enable --now workbench-staging-deploy.timer

# 7. Prove it — then wait one timer interval and prove it again, because the
#    first automatic deploy after a cutover is the one that finds what was
#    missed.
curl -s localhost:8787/healthz && curl -s localhost:8788/healthz
systemctl show workbench -p User -p WorkingDirectory
sudo journalctl -u workbench-staging-deploy -n 50 --no-pager  # the restore worked
tailscale serve status
```

**If it goes wrong**, the old checkout is untouched:

```sh
cd ~/workbench && WORKBENCH_DEPLOYMENT_ROOT="$PWD" ./install.sh
```

That re-renders the units back to the old location and owner and restarts. Your
pre-cutover database copy from step 3 is the belt to that pair of braces.

One thing the cutover does that is worth knowing about: `tasks.worktree_path` holds
absolute paths, and worktrees are not copied. Rows pointing into the old checkout have
their path cleared, so the next run makes a fresh worktree. Branches are untouched, so no
work is lost.

---

## Step 7 — Finish what the installer could not

`./install.sh` ends by printing this list, and `python -m workbench.doctor` prints it again
any time. Both are run **as the service account**, because that is whose credential and whose
key these are — `sudo -u` keeps your own `$HOME` and would answer about the wrong account, so
use `sudo -iu`.

Two steps are here rather than in the installer because both need a browser login against an
account no script can know about. Everything else it does for you.

**Sign the agent in.** Under a subscription the credential is an OAuth token in the service
account's home, and which account pays is decided by which user the unit runs as — so this
has to happen as that account and nowhere else.

```sh
sudo -iu workbench /srv/workbench/.venv/bin/python -m workbench.doctor --login
```

Choose the Claude subscription, not Console. `--login` passes `--claudeai` for exactly this
reason: Console authenticates perfectly well and bills the metered API, and nothing visible
in Workbench would change — the first sign would be an invoice.

**Add the deploy key.** The installer generated a keypair; it cannot authorise one. The
doctor prints the public half, along with the URL to paste it into:

Settings → Deploy keys → **Add deploy key** → paste → tick **Allow write access**.

A per-repo deploy key rather than an account-wide key, because an account key would grant
push to every repository this user can reach — and the account holding it runs
model-authored shell commands.

Then re-check until it is clean:

```sh
sudo -iu workbench /srv/workbench/.venv/bin/python -m workbench.doctor
```

It exits 0 when nothing is outstanding. Warnings and unknowns — no tailnet on this machine,
no network right now — do not set the exit code; only failures do.

Repeat both for staging, as `workbench-staging`. Two accounts means two logins, which is the
price of a staging agent that cannot reach production's checkout or database.

---

## Two things worth knowing in advance

**The status is posted by your PAT, so GitHub attributes it to your user account rather than
an app.** Anyone with push access could post a fake green `staging-acceptance`. On a
single-user private repository that is meaningless; it would matter the day you add a
collaborator.

**Requiring `staging-acceptance` on `main` means a hotfix cannot skip staging** without using
your admin bypass. That is the intended trade, but it is much better known now than
discovered at 2am.

## Turning things off

```sh
sudo systemctl disable --now workbench-deploy.timer          # stop deploying production
sudo systemctl disable --now workbench-staging-deploy.timer  # stop deploying staging
```

Deploys can still be triggered by hand afterwards with `sudo systemctl start workbench-deploy`.
