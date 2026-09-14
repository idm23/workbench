# Things this project taught me

Notes kept because the explanation was useful the first time and will be useful again.
Each entry is a thing that was opaque, what it actually is, and — where there was one —
the moment it bit.

Ordered roughly by layer: the machine, then the tools, then the services on top.

---

## systemd, systemctl, journalctl

**systemd is PID 1** — the first process the kernel starts and the ancestor of everything
else. It starts services in order, restarts them when they die, and tracks what is
running. Ubuntu ships it as the init system; it was not installed and cannot meaningfully
be removed.

**`systemctl` is a client, not the thing itself.** It sends requests to PID 1 over a
socket. This is why `systemctl daemon-reload` exists: systemd keeps units parsed in
memory, so editing a file on disk changes nothing until it is told to re-read.

**`journalctl` reads the journal**, systemd's log store. Anything a service writes to
stdout or stderr is captured automatically and tagged with the unit it came from — which
is why nothing here opens a log file. `logs.py` writes to stdout and systemd does the rest.

```sh
journalctl -u workbench -f              # follow live
journalctl -u workbench -n 50           # last 50 lines
journalctl -u workbench -p err          # errors and worse
journalctl -u workbench -b -1           # the previous boot
```

The journal survives reboots, rotates itself, and is binary — hence `journalctl` rather
than `cat`.

## Unit files are INI, and typos are silent

Sections in `[brackets]`, `key=value`, `#` for comments. Same family as `alembic.ini`.

- No quoting. The value is everything after the first `=`, verbatim.
- No spaces around `=`.
- Some keys legitimately repeat (`ReadWritePaths=` twice means both paths).
- **Unknown keys are ignored with a warning, not an error.** Typo `Restart=alwyas` and
  systemd shrugs; the service silently never restarts.

That last property is why `tests/test_units.py` shells out to `systemd-analyze verify`.
Breaking a template on purpose confirmed it catches `Unknown key name 'OnUnitActiveSecc'`
— a test that only ever passes proves nothing.

Reference: `man systemd.unit`, `man systemd.service`, `man systemd.timer`, and
`man systemd.exec` for the sandboxing menu.

## Two unrelated things are called "template"

`deploy/*.template` here are **ours**: plain unit files with `__PLACEHOLDER__` markers,
substituted by `install.py:render_unit()` at install time. systemd never sees a placeholder.

systemd's own **template units** use `@` in the filename and `%i` inside, so
`systemctl start workbench@8788` instantiates one file many times. Genuinely close to the
staging/production split here, and deliberately not used: the instances differ by repo
path, port, branch, and restore source, and `%i` gives exactly one string.

Two substitution systems sharing a word. Ours runs once at install; systemd's at unit load.

## PrivateTmp means the service cannot see /tmp

`PrivateTmp=yes` gives a unit its own `/tmp` and `/var/tmp`, invisible to everything else.
Good hardening, and a trap: a checkout under either path does not exist from inside the
service.

Cost a CI cycle. The test installed to `/tmp/workbench-deploy-cycle`, and systemd reported
only *"the control process exited with error code"* with an empty journal — because the
process never got far enough to log. `install.sh` now refuses a checkout under `/tmp` or
`/var/tmp` outright rather than letting anyone else find out that way.

## ProtectSystem=strict inverts the default

It makes the *entire filesystem* read-only for the process; `ReadWritePaths=` punches
holes. The app can write its database and nothing else — not `/etc`, not its own source,
not the home directory.

Worth widening deliberately when agents arrive rather than reaching for
`ProtectSystem=off`.

## SQLite in WAL mode cannot be copied with `cp`

With write-ahead logging, committed data lives partly in the `-wal` file, not the `.db`.
A plain copy of a live database can miss rows that are definitely committed, or capture a
torn version.

The fix is SQLite's own backup API — `sqlite3.Connection.backup()` — which is what the
staging snapshot restore uses. It also needs no `sqlite3` binary, which is not installed
on either machine.

## `git merge --ff-only` does not mean "refuse if dirty"

It refuses a fast-forward that would **overwrite a file you modified**. A commit touching
any *other* path merges straight over your working state.

The deployer relied on that for its "a dirty checkout is left alone" promise, and the
promise was only accidentally true — it held or not depending on what the incoming commit
happened to contain. Found by a test whose commit deliberately touched nothing the
checkout had edited. Now there is an explicit `git status --porcelain` check, and
untracked files are allowed through because a stray log is not work in progress.

## Python logging can fire at import time

`alembic.runtime.plugins` announces every autogenerate plugin it registers when the
library is *imported*, not when it is used. So merely importing something that touches
alembic emits seven lines.

Quieting it in `alembic.ini` fixed it for the alembic *CLI* — a different process — while
the deployer, which imports the library, kept logging. The giveaway was in the journal all
along: the server printed `setup plugin …` with no `INFO [logger]` prefix, meaning it was
going through this project's logging config, not alembic's.

Lesson: match the log *format* to the process that produced it before deciding where a
fix belongs.

## A permission mode is not a sandbox

`acceptEdits` sounds like the safe middle setting for a headless agent: it lets the model
edit files without waving through everything else. What it actually does is permit edits
and still gate `Bash` — so the agent writes the code, tries to `git commit`, gets stopped,
and has nowhere to send the prompt because the run is detached with nobody attached to it.
It retries. Thirty-three turns of that, ending with a full worktree, no commits, and a bill.

The mistake was reading permission modes as a security boundary. They are an *interaction*
design — which actions are worth interrupting a human for — and on a run with no human they
degrade into "which actions fail silently". A mode that gates Bash on a headless run does
not contain the agent, it just makes it useless.

So the execute phase runs with permissions bypassed, and the containment is somewhere else
entirely: an unprivileged service user with no sudo, working in a throwaway worktree whose
contents are recoverable from GitHub. The bound on an agent is the account it runs as.

The plan phase is the exception that proves the rule — it gets a real read-only plan mode,
because there read-only is the *product* rather than a restriction on it, so enforcement
and intent point the same way.

## A new session does not escape the cgroup

`start_new_session=True` is the usual advice for "spawn something that outlives me", and
it does exactly one thing: puts the child in a new session and process group, so a
terminal hangup or a `kill -- -PID` at the group no longer reaches it.

systemd does not kill by process group. `KillMode=control-group`, the default, kills every
remaining process in the unit's control group when the unit stops, and a forked child is
in that cgroup no matter how many sessions it has started. So a detached agent run is
still killed by a deploy restarting the app it was spawned from.

Escaping means leaving the cgroup — a transient scope via `systemd-run`, or a separate
unit — both of which need privileges an unprivileged service does not have. Worth knowing
before designing around "detached" as though it meant "survives".

The consolation is that the durable event log makes the difference smaller than it looks:
a killed run is recoverable reading rather than lost work.

## A self-updating deployer is always one deploy behind itself

The deployer pulls, then acts. But it imported its own code *before* pulling, so the run
that brings in a change to the deployer is the last run that does not perform it. The new
behaviour starts on the deploy after.

That is fine for a change to a step that already runs. It is not fine for a *new* step,
because the deploy that installs the code also reports success — the service restarts into
the new version, `/healthz` shows the new revision, everything looks landed — while the new
install step has never executed once. It then waits for an unrelated commit to come along.

Found the hard way: agent runs need a polkit rule, the deployer learned to write one, and
the deploy that delivered that knowledge did not use it. Every run failed with "interactive
authentication required" on a machine whose revision said the fix was deployed.

The general answer is to make installing *convergent* rather than event-driven — run it on
every tick, not only when a commit arrived. It is idempotent and compares before writing,
so an idle tick costs four template renders and no writes, and a unit deleted by hand comes
back on its own.

Same family as "it cannot install itself": a machine with no timer never fetches the commit
that would give it one. Anything that installs the mechanism it depends on needs a manual
first push or a convergent loop.

## Run the suite the way CI runs it

`python -m pytest` and `uv run pytest` are not the same command. `-m` puts the working
directory on `sys.path`; the console script does not. So a test importing something from
`scripts/` passes locally and fails in CI with `ModuleNotFoundError`, and the diff that
broke it looks innocent.

`pythonpath = ["."]` in `[tool.pytest.ini_options]` settles it for both, which is the
actual fix — a suite whose result depends on how it was started is worse than one that is
simply wrong, because the disagreement is invisible until something else fails.

The habit that would have caught it: verify with the invocation the workflow uses, not the
one that is convenient to type.

## git forks background work you did not ask for

`git commit` and `git push` can spawn `gc --auto`, which detaches and keeps writing into
`.git` after the command you ran has already exited. For a repository you keep, that is
the point. For one a script deletes a moment later, it is a race: `shutil.rmtree` scans a
directory, the detached process creates a file in it, and the `rmdir` fails with ENOTEMPTY
on a directory that was empty when it was looked at.

It shows up as an intermittent failure that never reproduces locally, because whether the
collision lands depends on timing and on directory-entry order.

Two fixes, and both are worth having. At the source, `gc.auto=0` and
`maintenance.auto=false` on any throwaway repository, so nothing detaches. At the sink, a
delete that retries and then gives up *without failing the test* — because deleting
scaffolding is not what the test is testing, and a suite that goes red over its own
temporary files is one people stop believing.

## Migrations against empty tables prove almost nothing

A migration that passes on an empty database routinely fails on real rows — a `NOT NULL`
column with no default, a unique constraint real data violates, a type change SQLite's
batch rewrite cannot perform on populated tables.

Two answers here: `test_migrations.py` seeds rows at the *previous* revision using raw SQL
(the ORM would insert today's shape, defeating the point), and staging restores a snapshot
of production before migrating.

## Squash from short-lived branches, merge from long-lived ones

Git computes a merge base from **ancestry, not content**. Squashing discards the parent
link: the new commit on the target branch has the same content as the source commits but
no lineage to them, so git genuinely does not know they are already there.

For a feature branch that is deleted after merging, that costs nothing and buys one tidy
commit instead of six saying "fix typo". For a long-lived branch compared against the
target on every cycle, it compounds — the merge base freezes at the last point the two
genuinely shared, and every subsequent pull request re-shows work the target already has.

Measured here after one squash promotion of `staging` into `main`: the next promotion
would have displayed **16 files and 1,419 lines changed when only 12 files and 1,071
lines actually differed**. Roughly 350 lines of it was the previous promotion, shown
again. That gap grows with every cycle.

**"Rebase and merge" has the same problem.** It replays the commits onto the target as
*new* commits with new hashes, so the originals on the source branch are still ancestors
of nothing. It looks like the tidiest option and breaks ancestry just as completely. Only
**"Create a merge commit"** preserves it, because that commit's second parent *is* the
source branch tip.

A second consequence, easy to miss: with merge commits, realigning the long-lived branch
onto the target is a **fast-forward**, which a `non_fast_forward` ruleset permits. After a
squash it could only ever be a force push, which that rule blocks. So the merge method
decides whether realignment is possible at all.

| | method | why |
|---|---|---|
| feature → `staging` | Squash and merge | one commit per pull request; the branch is disposable |
| `staging` → `main` | Create a merge commit | keeps `staging` an ancestor, so the merge base advances |

## GitHub rulesets vs branch protection

Both exist and either works, but **rulesets let you type an arbitrary status check name**.
Legacy branch protection only offers a search box listing checks GitHub has seen in the
last seven days — so a check that has never been posted is unselectable, which is a
chicken-and-egg when the code producing it is in the pull request you are trying to merge.

Required check names are the **job `name:` values** from the workflow. Rename a job and
the required check silently stops matching; the pull request waits forever on something
that will never report.

**`bypass_mode` matters more than it looks.** `always` lets an admin push directly with no
prompt — which is how a change reached `main` without going through staging. `pull_request`
still allows overriding a red check when merging, but blocks direct pushes. The
distinction worth wanting: override a check, not skip the pipeline.

**The ruleset here targets `~DEFAULT_BRANCH`, not `main` by name.** Changing the
repository's default branch would silently retarget it.

## Tailscale

- **`serve` publishes to the tailnet; `funnel` publishes to the internet.** Never funnel.
- **Serving a filesystem path needs root. Proxying a port only needs operator status**
  (`sudo tailscale set --operator=$USER`). Without an operator set, state-changing
  commands need root, and over a non-interactive SSH session with no TTY they *hang*
  rather than erroring. The hung process ignores SIGTERM.
- **Tailscale SSH can intercept connections** and require a periodic browser re-check,
  which also presents as a hang. Its ACL rule is `"action": "check"`; `"accept"` removes
  the prompt and the protection with it.
- Valid HTTPS needs **HTTPS Certificates** enabled in the admin console — it is what makes
  "Add to Home Screen" behave like a real app.

## A process that pulls new code must not lazily import it

The deploy that shipped a dedicated service account died like this:

```
File ".../deploy.py", line 482, in refresh_units
File ".../install.py", line 39, in <module>
    from workbench.config import (
ImportError: cannot import name 'agent_git_identity' from 'workbench.config'
```

Every one of those names exists. The new `install.py` asks for a symbol the new
`config.py` defines, and importing both together works perfectly — which is why CI, the
container harness and a local run all passed.

The deployer is a long-running process that changes the code underneath itself. It
imported `deploy` and `config` at startup, from the *old* commit. Then it fast-forwarded
the checkout, so the files on disk became new. Then `refresh_units()` did a lazy `from
workbench.install import ...` — and Python loaded the **new** `install.py` off disk while
`workbench.config` was already cached in `sys.modules` from the **old** commit. New
module, old dependency, one process.

The tell was in the traceback: line numbers pointed at the wrong statements, one of them
a docstring. Python renders frames by reading the file *now* while executing what it
loaded *then*, so a mismatch between the two is visible in the trace itself.

**A deferred import is a decision about which version of the code runs, not just when it
loads.** Anywhere a process outlives a change to its own source, every module it will
ever need must be imported before the change lands. `deploy.py` now imports
`workbench.install` at module scope, so both halves are always from the same commit — the
old one — and the deploy after it converges. That the deployer runs one commit behind is
already the documented design; the lazy import was quietly opting one module out of it.

The preflight (`import workbench.app` in a throwaway subprocess) could not have caught
this, and it is worth understanding why: in a *fresh* process the new code imports
fine. The failure only exists in a process that straddles the pull, so no amount of
checking the new code in isolation would find it.

## An action that only runs on the tick that changed something will eventually not run

Same deploy, second failure, and the more expensive one. The fast-forward succeeded
before the crash, so the checkout was left at the new commit. Every tick after that
returned `AlreadyCurrent` — and acceptance ran only on a tick that had *advanced*. So the
commit was reported on by nobody: no status, red or green, ever again.

Nothing looked wrong. The service was healthy, serving the new revision, units correct,
journal clean from the next tick onward. Only GitHub knew, by staying silent, and silence
is what promotion waits on.

The fix is the same shape as the one that made unit installation convergent: decide from
*state* rather than from *what just happened*. A local marker file records the revision
acceptance last reported on, and any tick where that disagrees with `HEAD` runs it. One
outcome is deliberately not recorded — a verdict that ran but never reached GitHub —
because that is the single case where retrying is what fixes it.

**Every step conditioned on "something changed this tick" is a step that silently stops
happening the first time a run dies between the change and the step.**

That sentence was written with the note that two had been found and "there is no third at
the time of writing, which is not the same as there not being one." The third turned up
forty minutes later, on the first production deploy after the same promotion, and it had
the worst symptom of the three: the restart.

Production pulled the commit, crashed on the same import, and stopped before
`systemctl restart`. New code on disk, new units on disk, and the old process still
serving requests — with the checkout already advanced, so every later tick was
`AlreadyCurrent`, converged the units, and never restarted anything. `/healthz` honestly
reported a revision five commits behind what the repository said was deployed, and it
would have stayed that way indefinitely.

So the restart converges too, from a marker recording which revision the running service
was started into. Two details in that are load-bearing:

- **It is recorded after the health check, not after the restart returns.** A service that
  was started and never came up is not one that is serving.
- **A machine with no marker is treated as _not_ stale, and seeded on the next idle tick.**
  The opposite reading is tempting and worse: a machine where the marker cannot be written
  would restart the service every five minutes forever, which is a bigger failure than the
  one being fixed. Seeding happens on a tick where nothing was pulled, so the service is
  current by definition, and a later failed deploy then has an old revision to disagree
  with.

The app's own `/healthz` revision looks like it would serve instead of a marker, and it
does not. That value is cached for the life of the process at its first request, so a
service that started but had not yet answered anything when a pull landed would report the
*new* revision while running the old code — leaving exactly the stuck state the check
exists to clear.

Three for three. The general lesson is not about deploys: **any convergent loop should
decide from the state it can observe, never from what it happened to do this iteration.**
The two are equivalent only while nothing ever fails in between.

## A monotonic systemd timer does not survive being restarted

Both deploy timers reported `enabled` and `active`. Neither had fired for half an hour,
and neither ever would again:

```
$ systemctl status workbench-deploy.timer
     Active: active (elapsed) since Sat 2026-08-29 18:22:10 UTC; 28min ago
    Trigger: n/a
```

`active (elapsed)` with `Trigger: n/a` is systemd for "this timer has no future". The
schedule was:

```ini
OnBootSec=2min
OnUnitActiveSec=5min
```

Both are *monotonic* — each measures from an anchor. `OnBootSec` measures from boot, and
this machine had been up seventeen days, so that moment is long past and cannot recur.
`OnUnitActiveSec` measures from the last activation of the service the timer triggers,
**counting only activations since the timer itself started**. Restart the timer and there
have been none — and the thing that would produce one is the timer firing. No anchor, no
next elapse, no deploys.

It chains correctly for as long as the timer runs untouched from boot: the boot trigger
fires once, that activation anchors the interval, and each firing re-anchors the next. The
whole arrangement is one unbroken chain from a single event seventeen days earlier, and
restarting the unit breaks it permanently.

Which would be a curiosity, except `install_units` restarts this timer **on purpose**
whenever the rendered file changes, with a comment explaining that a changed interval
would otherwise not take effect until reboot. The one code path written to update the
schedule was the path that silently switched deployment off.

`Persistent=true` was already in the file, already documented as catching up a check
missed while the machine was off, and doing nothing whatsoever: it only applies to
`OnCalendar=` timers. Two bugs, one of them decorative, sitting next to each other for
weeks.

`OnCalendar=*:0/5` has no anchor to lose. Restarting it, reinstalling it, or reloading
systemd all leave the next occurrence where it was, and `Persistent=true` starts meaning
what its comment always claimed.

**The test asserted the bug.** It checked `"OnUnitActiveSec=" in timer`, which is true of
exactly the configuration that breaks. A unit-file test that asserts a directive is
*present* pins the spelling; what was needed was a property — *this timer still fires
after it is restarted* — which is expressible as the absence of any anchor-dependent
trigger. Worth asking of any assertion over configuration: does this describe a behaviour,
or transcribe the file?

One practical note for writing those assertions: these templates carry long comments that
name the directives they argue against, so "absent" has to mean absent from the
configuration rather than unmentioned in the prose. `directives()` in `test_units.py`
strips comments for exactly that reason.

### And a coda, twenty minutes later

The fix for the above introduced `OnCalendar=__SCHEDULE__` and a matching entry in the
installer's replacement table. It deployed as:

```
OnCalendar=__SCHEDULE__
Failed to restart workbench-staging-deploy.timer: Unit ... has a bad unit file setting.
```

The deployer imports its own code before it pulls, so the *template* comes from the new
checkout while the *renderer* is the old one — which had never heard of `__SCHEDULE__` and
left it verbatim. Invalid unit, failed restart, timer dead a second time in an hour, by a
different mechanism, in the commit that fixed the first one.

**Adding a placeholder is backward compatible. Renaming one, or requiring a new one in an
existing template, is not.** An older renderer silently emits it as literal text, and unit
files fail late and quietly rather than at parse time.

The schedule was never configurable — it is baked in at install time by design — so it did
not need to be rendered at all, and is now literal text in the template. That is worth
generalising: **a value that is not actually variable should not be a placeholder**, because
every placeholder is a contract between two versions of the code that meet only during a
deploy.

## A credential that reports itself signed in can still be dead

The first real conversation runs on the server worked. Two of them, seventeen hours apart.
The third failed with `401 OAuth access token has expired. Re-authenticate to continue.`,
after the CLI retried twice on its own — and every page of the app was still green while it
happened.

**`claude auth status --json` does not answer "does this work".** It answers which account
and by what method, and both stay true long after the credential has stopped working:

```json
{"loggedIn": true, "authMethod": "claude.ai", "email": "...", "subscriptionType": "pro"}
```

There is no expiry field in it, so a doctor built on that probe reports `ok` for a machine
where every run fails at authentication. That is worse than having no check, because the
banner exists precisely so that nobody has to guess.

The expiry is in `~/.claude/.credentials.json` instead, and it is two dates rather than one:

```
expiresAt             the access token, eight hours
refreshTokenExpiresAt the renewal window, about a fortnight
```

**Renewing does not extend the renewal window.** Both timestamps in that file are written by
the same refresh response — they agree to the millisecond — and yet `refreshTokenExpiresAt`
was not a fortnight from that refresh. It is anchored to the original interactive login. So a
headless server signed in once will fail roughly two weeks later no matter how often it runs,
and "it renewed fine yesterday" says nothing about tomorrow.

Two consequences for the shape of the check. The access token expiring is *routine* and must
not be reported as a problem — a run past the eight-hour mark renews without anyone noticing,
so a check that failed on `expiresAt` would put a red banner on a working machine every night.
And the fix cannot be automated: the SDK exposes no auth surface, `claude auth` has only
`login`/`logout`/`status`, and `auth status` does not itself renew. The only unattended
renewal that exists is the one a run already does for free. Past the window it needs a
browser, which is exactly the class of step `doctor.py` was written to make *discoverable*
rather than automatic.

Worth knowing for later: `claude setup-token` mints a long-lived subscription token
(inference-only scope, supplied as `CLAUDE_CODE_OAUTH_TOKEN`), which is the shape a headless
box actually wants and would retire the fortnightly cliff.

## A server-rendered gate is a snapshot, and sometimes of the wrong instant

The reply box on a run page was gated on `{% if run.status == 'running' %}`, which is the
right condition and was still never true. Starting a conversation creates the row, asks
systemd to start the runner, and redirects to the run page immediately — so the page renders
while the run is `queued`, a moment *before* the condition becomes true. The runner is a
different process; there is nothing to wait for.

Everything after that worked as designed and made it worse. The SSE stream connected and
appended events, so the page looked alive. But the stream only ever appended `<li>`s — it
never revisited the form. And the one reload the page does perform fires on `end`, when the
run is over and the box correctly should not be there. The result was a control that existed
in the template, passed its tests, and was unreachable from the button that leads to it.

**The bug is not the condition, it is that the condition was evaluated once.** This is the
same shape as the deploy rule in `CLAUDE.md` — *every step converges rather than firing on a
change* — arriving in the web tier. Anything rendered from state that is about to change
needs either a re-render or something that keeps it in step; a gate resolved server-side at
page load is a snapshot, and here it was reliably a snapshot of the instant before.

The fix is to render the form for any non-terminal run, hidden, and let the `status` events
already on the stream reveal it — the same events that already drive the live dot. Worth
noting the test that would have caught it: every existing test asserted on a run *already*
running or already finished, and none on a run in the state the page is actually first
rendered in.

## A fallback that works is how a GPU node spends a month encoding on its CPU

Sunshine probes every encoder it knows at stream time, in order, and takes the first that
opens. On this project's node the first one did not:

```
Error: [h264_nvenc] Driver does not support the required nvenc API version. Required: 13.1 Found: 13.0
Info: Encoder [nvenc] failed
Error: Couldn't open /dev/dri/renderD128: Permission denied
Info: Encoder [vaapi] failed
Info: Found H.264 encoder: libx264 [software]
```

Then it streamed 1080p perfectly, on the CPU, and printed all of that under its own banner
reading `// Ignore any errors mentioned above, they are not relevant. //`. Which is honest:
that banner is there because the probe *is* supposed to generate errors, and on a node with
no GPU the software encoder is the correct answer rather than a degraded one.

**The failure has no error, because it is the same code path as the success.** Nothing
distinguishes "fell back correctly" from "fell back catastrophically" except what the
machine was for, which no log line knows. `nvidia-smi` is no help either — it reported a
healthy driver the entire time, because the driver *was* healthy; it was one API revision
short of what the ffmpeg inside Sunshine was compiled against, and nothing surfaces that
comparison. The whole gap was `nvidia-driver-595` where `nvidia-driver-610` was wanted, both
sitting in the same apt repository.

The check that catches it is three lines: read back which encoder was chosen and warn if the
answer is `[software]`. That is the same shape as `claude auth status` not answering "does
this work" — a probe that reports a *component* being fine while the thing built from it is
not, and the fix in both cases is to ask the question you actually care about.

The `render` group in that second error is worth its own note. `/dev/dri/card*` is group
`video` and `/dev/dri/renderD*` is group `render` — two groups on the same directory, and
the installer granted only the first. It cost nothing visible, because NvFBC needs only
`card*`, so the missing group silently removed a *fallback* rather than the primary path.
Those are the ones that sit there for months: a permission that only matters on the day
something else has already gone wrong.

## `systemctl --user enable` will happily link a unit to a target that never runs

Sunshine ships `WantedBy=graphical-session.target`. A headless gaming node has no graphical
session and never will — its X server is a *system* unit, and nobody logs in. So:

```
$ systemctl --user is-enabled app-dev.lizardbyte.app.Sunshine.service
enabled
$ systemctl --user is-active app-dev.lizardbyte.app.Sunshine.service
inactive
```

`enable` did exactly what it was asked: wrote the symlink into
`graphical-session.target.wants/`. That target is simply never activated, so the unit could
not start at boot, and `is-enabled` has no opinion about whether the target it linked into
is reachable.

**What hid it for so long was `enable --now`.** The `--now` half starts the unit
imperatively, right there in the install, so every install ended with Sunshine running and
every check afterwards agreed. Only a *reboot* revealed it, and a node that streams to a
television is rebooted rarely and tested immediately after an install — the two moments
where it always looked fine.

The fix is `systemctl --user add-wants default.target <unit>`, which is the target a
lingering user manager actually reaches. Worth knowing: an `[Install]` section inside a
drop-in is not a reliable way to change this, because `enable` reads `[Install]` from the
unit file proper; `add-wants` writes the one symlink directly and says so.

This is the third instance of the same family in this project — units the installer created
that a deploy never converged, the polkit rule, the push keypair — and the first where the
thing that lied was not *whether* something was enabled but *what it was enabled into*.

## Configuration that is parsed, ignored, and never mentioned again

Sunshine's `global_prep_cmd` — the hook the entire gaming switch hangs on — went into
`apps.json`, on the strength of documentation describing exactly that key at exactly that
top level. Sunshine read the file, listed the app, streamed from it, and ignored the key,
because this version reads global prep commands from `sunshine.conf`. You can tell which
from the web UI's own source: `global_prep_cmd` is rendered by the *Configuration* page's
bundle, not the *Applications* one.

There is no warning for an unrecognised key, and there is no reason to expect one — JSON
config with unknown fields ignored is the normal, forgiving thing. The result was a switch
that had never fired once, on a node where everything about it was correct: the unit
existed, the polkit rule matched, and running `systemctl start workbench-gaming` by hand as
the gaming user worked perfectly. Every individual piece passed its own test.

**The test that would have caught it could not have been a unit test**, because the thing
being asserted is what a third-party binary does with a file. What caught it was streaming
for real and then asking a question about a *different* process: was Ollama stopped? The
general lesson is that config handed to someone else's program is only verified by observing
that program's behaviour, and `journalctl -u <the thing it should have done>` is the
assertion.

Confirmation of the fix came from the same place, and reads better than any test:

```
Info: Executing Do Cmd: [systemctl start workbench-gaming]
```

## A tool that cannot be scripted is the wrong tool, however well it works

`moonlight-qt` streams beautifully. It is also undrivable from a script, in three
independent ways, each discovered only after the previous one was worked around:

- `--video-decoder software` is parsed and ignored.
- `videodecoderselection` in its own `Moonlight.conf` is ignored by the `stream`
  subcommand too, so there is no file to write either.
- Its pairing PIN appears only in a GUI dialog — on a machine whose entire purpose is to
  have no desktop, behind a modal warning that needs a keyboard to dismiss.

The third is the one that settled it. Pairing is a one-time step, and this project already
accepts a handful of those. But it was not *manual*, it was **impossible**: there was no
way to obtain that PIN over SSH at all. The machine has no working keyboard path and its
only display is a television in another room.

Moonlight Embedded prints the PIN to stdout:

```
Please enter the following PIN on the target PC: 1697
```

That is the whole difference, and it is worth more than any amount of polish. A headless
box needs a client whose interface is text. Note the cost being accepted here: Moonlight
Embedded is packaged for nothing — its own apt repository serves a `trixie` suite
containing no such package — so `install_client()` builds it from source, about two
minutes on a Pi 4. That is a real maintenance burden, taken deliberately, because the
alternative could not be operated at all.

## Raspberry Pi OS Trixie provisions with cloud-init, not `custom.toml`

Writing `custom.toml` to the boot partition of a 2026-06 Trixie image does nothing. The
file is never read, the image boots into its interactive first-boot wizard, and on a
headless machine that is a brick — a working install that cannot be logged into.

The give-away was sitting on the stock boot partition all along:

```
meta-data       dsmode: local, instance_id: rpios-image
network-config  netplan-compatible, applied on first boot
```

That is cloud-init's NoCloud datasource. `custom.toml` is the older Imager mechanism.
The file to write is `user-data`, `#cloud-config`, with `hostname`, `users:` carrying
`ssh_authorized_keys`, and `ssh_pwauth: false`.

**And `user-data` still does not enable SSH.** Pi OS ships `sshd` disabled and switches it
on from `sshswitch.service`, which looks for a file literally named `ssh` on the boot
partition. Configuring sshd is not starting it: the first boot produced a correctly
provisioned machine, with the right user and the right key, refusing connections on port
22. Two boot cycles were spent learning that, one per mechanism.

The general lesson is narrower than "read the docs": **check what the artefact in front of
you actually consumes** before writing config for it. The stock image listed its own
answer in a directory listing.

## Two interfaces on one subnet is a LAN-wide fault, not a local one

A Raspberry Pi with `eth0` and `wlan0` both on `192.168.1.0/24`, Wi-Fi in `DORMANT` state
and holding the route:

```
192.168.1.155 via wlan0   →  100% packet loss
eth0                      →   41% packet loss
```

Two things are worth keeping from this. The first is that it presented as *audio*: the
stream's video degraded gracefully via FEC while the audio crackled and dropped out, so
the obvious diagnosis was audio — realtime priority, buffer sizes, `rtkit`. All of that
was a red herring; on a clean network the same stack is silent-clean with none of it.

The second is that it was not confined to the offending machine. Another host on the same
switch became unreachable for seventeen minutes while its own journal recorded no network
event whatsoever — no carrier loss, no link-down, nothing. MAC-table confusion from one
machine claiming a subnet twice is enough to disrupt its neighbours, and the neighbour has
no way to see why.

**Check the physical and link layer first when a working setup breaks after being moved.**
That is exactly when it is most likely, and it is the layer that reports nothing.

## Small ones

**`curl -I` sends HEAD**, and FastAPI does not auto-add HEAD to a GET route. A `405` with
`allow: GET` from a health check means the app is fine and the request was wrong.

**`curl -X POST` forces POST through a 303 redirect** instead of letting curl switch to
GET, turning every check into a `405`. `httpx` follows 303 correctly by default, which is
why the smoke test is Python.

**`git mv` preserves history** across a reorganisation; `mv` followed by `git add` usually
does too, but only because git infers renames after the fact.

**Tests inside a package ship in the wheel** unless excluded. `exclude = ["**/tests"]` in
the hatch config keeps them out of the venv and off the server.
