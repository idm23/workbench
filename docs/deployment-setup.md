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
  ticked. Then **Add checks** and type each of these exactly, pressing `+` after each:
  - `Lint, types, and tests`
  - `Fresh install on clean Ubuntu`
  - `Deploy cycle on systemd`

> These three strings are the `name:` values of the jobs in `.github/workflows/ci.yml`.
> Renaming a job there silently stops the required check from ever matching, and the pull
> request waits forever on something that will never report. Change them together.

Do **not** add `staging-acceptance` yet — see step 5.

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

- **Contents** → **Read-only**
- **Commit statuses** → **Read and write**

Everything else stays *No access*. The agent slice will later want **Contents: Write** and
**Pull requests: Write**; add them when that lands, not now.

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
