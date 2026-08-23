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

## Recommendation

**Option 1.** It is the smallest privilege that actually solves it: one line in
`install.sh`, no standing policy, and it removes a whole category of "my run vanished"
that would otherwise need explaining every time. Option 2's per-run units are genuinely
nicer to operate, but a polkit rule granting unit-start rights to the account that runs
model-authored commands is a bigger thing to hand over than lingering, and the operational
niceness can be added later if it turns out to be missed.

Option 3 is more defensible than it first looks, because resume means an interrupted run
costs time rather than work. It is the right answer if lingering proves awkward on this
machine.

Whichever is chosen, `runs/runner.py` does not change: it takes a run id, records its own
outcome, and assumes nothing about who started it. That is deliberate — the decision here
is about the *spawn*, and it should stay swappable while the answer is still being tested
against a real machine.
