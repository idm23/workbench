# Running an agent

How a task actually gets worked by a model on this machine: what exists while it runs,
what can kill it, and what we do about that.

Written because the last part is genuinely counter-intuitive. The obvious way to keep a
background process alive does not work under systemd, and the reason has nothing to do
with the code — so it is worth understanding before reading `runs/runner.py` and
concluding it is written strangely.

## What a run is, physically

Starting a run creates four things.

**A worktree.** `data/worktrees/task-12-add-route-tests/`, a real checkout of the project
on a branch of its own. The agent's working directory, and the only place it is expected
to write. One per *task*, not per run, because a plan run and the execute run after it are
two attempts at one piece of work.

**A process.** `python -m workbench.runs.runner 42`, where 42 is a row in `runs`. It
prepares the worktree, drives the backend, and records everything. It is the only thing
that writes to that run.

**A conversation.** Held by the backend's own CLI, which the runner starts as a child
process. This is what actually talks to the model. Workbench never sees it directly — it
gets an opaque `resume_token` back, which is enough to continue the same conversation in
the execute phase without re-deriving the plan.

**An event log.** Rows in `run_events`, one per thing the agent said or did, committed as
they arrive rather than at the end.

The process tree looks like this:

```
uvicorn                                   the web app
└── python -m workbench.runs.runner 42    the runner
    └── claude                            the backend's CLI
        └── bash, git, ...                tools the agent invokes
```

## Why the runner is its own process

Three reasons, and only the third is about deploys.

A run takes minutes, sometimes longer. Holding one inside a web request would mean the
request never returns, and a phone that locks its screen mid-run would drop it.

The web tier has to stay a *pure reader*. The page you look at, the stream you reconnect
to, the run you read back a week later — all of those are queries against `run_events`. If
the run lived in the web process, refreshing the page and rereading history would be two
different mechanisms.

And any restart of the web app would take the run with it. Which brings us to the part
that is not obvious.

## The part that is not obvious

Python's `subprocess.Popen(..., start_new_session=True)` is the standard way to say "this
child should outlive me". It is in every recipe for daemonising something. It does not
work here, and understanding why requires two ideas that sound like the same thing.

**A process group** is an old Unix concept. It exists so a terminal can signal a job:
pressing Ctrl-C sends SIGINT to the foreground process group, and closing a terminal sends
SIGHUP to its session. `start_new_session=True` puts the child in a fresh session and
process group, so those signals no longer reach it. That is what "detached" classically
means, and against a terminal it works perfectly.

**A control group** — a cgroup — is the kernel's containment and accounting tree, and it
is what systemd actually manages. Every process a unit starts is placed in that unit's
cgroup, and every child inherits it. **Nothing a process does to its own session changes
its cgroup.** They are unrelated mechanisms that both get described as "detaching".

When `workbench-deploy.service` restarts `workbench.service`, systemd stops the unit. The
default `KillMode=control-group` means exactly what it says: SIGTERM to *every process in
the unit's cgroup*, then SIGKILL to whatever is left after `TimeoutStopSec`. It does not
walk the process tree and it does not care about sessions. It kills a cgroup.

```
system.slice
└── workbench.service              ← systemd stops this unit
    ├── uvicorn                        killed
    └── runner  (new session!)         killed anyway — same cgroup
        └── claude                     killed
            └── git, bash              killed
```

So a run started from the web app dies when the web app restarts, no matter how detached
it is in the Python sense. And deploys are automatic here — `workbench-deploy.timer` fires
every five minutes and restarts the service whenever a commit has landed — so this is
ordinary rather than exceptional. Merging an agent's own pull request is precisely the
moment another agent run is most likely to be in flight.

You can watch the distinction yourself:

```console
$ cat /proc/self/cgroup
0::/user.slice/user-1000.slice/user@1000.service/app.slice/vte-spawn-....scope

$ systemd-run --user --scope --quiet -- cat /proc/self/cgroup
0::/user.slice/user-1000.slice/user@1000.service/app.slice/run-r3ed76....scope
```

The first inherits its parent's cgroup. The second is in a new one, which is the whole
trick.

## What is already true, whatever we choose

A killed run is not lost work. This is worth stating plainly, because it sets how much the
remaining problem is actually worth.

- **Commits survive.** The agent commits as it goes, into a worktree on its own branch.
  Nothing about killing the process removes them.
- **The log survives.** Every event is its own committed transaction, so the run reads
  back exactly as far as it got.
- **The ending is recorded.** The runner catches SIGTERM and writes `cancelled` with a
  reason, rather than leaving a row that says `running` forever with a pid that now
  belongs to something else.
- **The conversation survives.** `resume_token` and the worktree both live on the *task*,
  so a cancelled run can be continued rather than restarted from nothing.

So the question is not "how do we avoid losing work". It is "how often is a run
interrupted, and how annoying is that".

## The three ways to answer it

### 1. Leave the cgroup: a transient scope

Start the runner with `systemd-run --user --scope` instead of directly. systemd creates a
new scope unit for it under the *user* manager, which is a different cgroup to
`workbench.service` — so stopping that service does not touch it.

```
system.slice
└── workbench.service          ← stopped by a deploy
    └── uvicorn                    killed

user.slice
└── user@1000.service
    └── run-r3ed76….scope      ← the run, untouched
        └── runner → claude
```

**Needs:** `loginctl enable-linger` for the service user, so its user manager runs without
anyone being logged in, and `XDG_RUNTIME_DIR` in the unit so the app can reach that
manager. Lingering is a one-time root action, so it belongs in `install.sh` — a setup step
outside that script is exactly what the reproducibility rule forbids.

**Costs:** one more thing that has to be true on the machine. If lingering is off, runs
fail to start rather than failing later, which is the better direction but still a failure.

### 2. A unit per run

Define `workbench-run@.service` and start `workbench-run@42.service` over D-Bus. Each run
becomes a first-class systemd unit.

**Gains:** `systemctl status workbench-run@42`, per-run journald logs, and declarative
limits — `RuntimeMaxSec` would cap a runaway agent at the systemd level rather than
relying on the backend's turn limit.

**Costs:** an unprivileged service cannot start units without permission, so this needs a
polkit rule installed as root. That is a larger grant than lingering: it is a standing
policy saying this account may ask systemd to start things. Given the account in question
runs model-authored shell commands, that is worth being deliberate about.

### 3. Accept it

Change nothing. A deploy during a run kills it, the reaper marks it, and you start it
again — resuming the conversation, since the token and worktree are on the task.

**Costs:** nothing to install and nothing new to understand. The failure mode is an
occasional "why did that stop", answered by the `cancelled` reason in the log.

**When it bites:** merging a pull request while another run is in flight. That is not a
rare accident in a tool whose purpose is agents that open pull requests — it is the normal
working loop.

## The decision: a unit per run

**Option 2**, with the polkit rule scoped as narrowly as polkit allows.

The reasoning that settled it was not the deploy problem — option 1 solves that
just as well — but where this is going. Workbench is meant to grow into something
that dispatches work across a small pile of machines, some with GPUs. A unit is a
*job*: addressable by name, inspectable with `systemctl status`, stoppable,
loggable, and limitable. That is the right shape to grow into, and a bare process
is not.

Two corrections came out of that conversation and are worth keeping, because both
are easy to get backwards.

**A systemd unit is not remote dispatch.** systemd is an init system, not a
scheduler; there is no "run this unit over there". What makes another machine
cheap later is the *seam*, not the mechanism: `runs.executor` records where a run
ran, `runs.handle` is opaque and meaningful only to that executor, and a remote
node arrives as a third implementation of `Executor`. Unit-per-run is then the
local half of that story — including on the far node, which will very likely run
its own jobs as units too.

`executor` cannot be backfilled, which is the same argument that put `backend` on
the row. The moment a second executor exists, every earlier row is ambiguous
without it.

**Unit-per-run does not require polkit — but the useful version does.** A named
transient unit can be created in the *user* manager with no privilege at all:

```console
$ systemd-run --user --unit=wb-demo --property=MemoryMax=256M -- sleep 20
$ systemctl --user show wb-demo.service -p ControlGroup -p MemoryMax
ControlGroup=/user.slice/user-1000.slice/user@1000.service/app.slice/wb-demo.service
MemoryMax=268435456
```

Real unit, real cgroup, real limit, no root. What decides against it is which
controllers the user manager was delegated:

```console
$ cat /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/cgroup.controllers
memory pids
```

No `cpu`, no `io` — `CPUQuotaPerSecUSec` was accepted and silently ignored. And
device policy, which is how GPU access would eventually be scoped, is system-unit
only regardless. For jobs whose whole point is heavy resource use, that is
disqualifying. (Delegation defaults vary by version; the figures above are
systemd 249. Worth re-checking on any machine before relying on it.)

## What that costs, and how it is bounded

An unprivileged account cannot ask the system manager to start anything, so this
needs a polkit rule. That is a real grant, and the account receiving it is the one
that runs model-authored shell commands — so it is written as narrowly as the
mechanism permits:

```javascript
polkit.addRule(function (action, subject) {
    if (action.id !== "org.freedesktop.systemd1.manage-units") return;
    if (subject.user !== "workbench") return;
    if (/^workbench-run@[0-9]+\.service$/.test(action.lookup("unit") || "")) {
        return polkit.Result.YES;
    }
});
```

One action, one user, and a unit pattern anchored at both ends with a digits-only
instance. Everything else falls through to polkit's normal rules, which is a
refusal. The rule file is named per instance for the same reason the units are —
production and staging grant different patterns, and a shared filename would mean
whichever installed last silently revoked the other's ability to start runs.

`install.sh` writes both the template unit and the rule, so the reproducibility
promise still holds: a clone and one script, with no "and then add a polkit rule"
step living in someone's memory.

## What polkit is, and why the rule is shaped that way

Worth writing down because it is obvious while you are holding it and gone in three
months.

**polkit is the authorisation layer for privileged operations requested over D-Bus.** Not
authentication — it does not care who you are in the login sense — and not file
permissions. It answers a third question: *an unprivileged process owned by user X is
asking a privileged daemon to do Y; should the daemon comply?*

It exists because of a structural shift. The old way to let a normal user do a privileged
thing was a setuid-root binary, which is a large and permanent attack surface. The modern
way is a daemon that stays root while unprivileged clients *ask* it over D-Bus — at which
point the daemon needs a policy for who may ask what. polkit is that policy engine, shared
by systemd, NetworkManager, udisks and others.

### What happens when Workbench starts a run

`systemctl start workbench-run@42.service` starts nothing itself. It sends a `StartUnit`
method call to **PID 1** over the system bus. PID 1 is root and will not act on an
unprivileged request without checking, so it asks polkitd whether this uid may perform
`org.freedesktop.systemd1.manage-units` on that unit.

systemd's declared default for that action is not permissive:

```console
$ pkaction --action-id org.freedesktop.systemd1.manage-units --verbose
  implicit any:      auth_admin
  implicit inactive: auth_admin
  implicit active:   auth_admin_keep
```

`auth_admin` means "prompt for an administrator's password". A system service has no
session, no terminal, and nobody to type it. Asking polkit directly what it would say to a
process like ours confirms it:

```console
$ pkcheck --action-id org.freedesktop.systemd1.manage-units --process $$
Authorization requires authentication and -u wasn't passed.
exit=2
```

Denied. So the rule is not belt-and-braces: without it every run fails at the moment it is
started.

### The rule is four filters and a refusal

In order, and all of them must pass:

1. The action must be `manage-units`. Not `manage-unit-files`, not `reload-daemon`.
2. The subject must be the service user.
3. The unit must match `^workbench-run@[0-9]+\.service$` — anchored at both ends, with a
   digits-only instance.
4. Anything that reaches the end returns `undefined`, meaning "no opinion", and polkit
   falls through to the `auth_admin` refusal above.

**The unit name cannot be supplied by the caller**, which matters more than it looks.
polkit only accepts operation details from the action's owner:

```console
$ pkcheck --action-id ... --detail unit workbench-run@42.service
Only trusted callers (e.g. uid 0 or an action owner) can use CheckAuthorization()
and pass details
```

So the value the rule matches on is systemd's own view of what is being started, not a
string the requesting process controls. There is no spoofing it with a cleverly crafted
argument — which is exactly the property that makes matching on it worth doing.

### Why not sudoers

A narrow entry would also work and is more familiar:

```
workbench ALL=(root) NOPASSWD: /usr/bin/systemctl start workbench-run@*.service
```

polkit wins here for one specific reason: **sudoers matches on argv text, polkit matches on
structured data from systemd.** Command-pattern wildcards are a well-known source of
escapes, and the account on the other end of this grant runs model-authored shell commands.
Matching systemd's own notion of which unit is starting is a smaller thing to get wrong. It
also leaves "the service user has no sudo" true, which is easier to reason about than "no
sudo except this one line".

### When polkit stops being involved

- **If runs moved to the user manager.** That manager belongs to the user, so no privileged
  daemon is in the path and no rule is needed. That is the option rejected above over
  controller delegation; if that ever changes, the rule goes with it.
- **If the service ran as root** — which is the thing the unprivileged account exists to
  prevent.
- **If a run moved to another machine.** That node's executor answers for itself, and this
  rule becomes local-only.

### The one link not proven here

The rule depends on `action.lookup("unit")` returning the unit name. That is the documented
systemd pattern, but verifying it needs a rule that logs what it sees, which needs root on
a real machine.

The failure mode is safe. If the detail is absent the regex fails, the rule returns
`undefined`, and polkit refuses — so a broken grant surfaces as "access denied" in the run's
`error` column on the first attempt, never as a silent widening.

## Watching one

The same split explains how you watch a run, and why it is a query rather than a
subscription.

Nothing in the web process can be notified when the agent says something. The runner is a
different process, in a different cgroup, quite possibly started before this web process
existed — and SQLite has no LISTEN/NOTIFY. The only thing the two share is the table.

So `GET /runs/42/events` polls it: everything with `seq >` what the reader has already
seen, once a second, framed as server-sent events. That sounds crude and is exactly right,
because it makes resumption free. A reader that says "I have seen up to 41" can always be
told the rest, whether it disconnected a second ago or slept through the whole run. The
browser sends `Last-Event-ID` by itself on reconnect; the page passes `?after=` on first
load, having already rendered the past server-side.

Two consequences worth keeping in mind:

- **The page renders without the stream.** A run read back a week later has no stream to
  open, and a page that is blank until JavaScript connects is blank when JavaScript fails.
  The stream only ever adds to what the server already rendered.
- **The reader sweeps past the end.** `finish_run` commits the terminal status and *then*
  appends the status event, so a stream that stopped the instant it saw a terminal status
  could miss the last thing that happened. Sweeping for a few more polls makes the reader
  correct whatever order the writer used — which is the property that survives someone
  reworking the writer later.

## What is still true

`runs/runner.py` did not change for any of this. It takes a run id, records its
own outcome, and assumes nothing about who started it — which is what let the
decision be made after the runner was written, and what will let a GPU node be
added without touching it.
