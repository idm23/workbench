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

## Migrations against empty tables prove almost nothing

A migration that passes on an empty database routinely fails on real rows — a `NOT NULL`
column with no default, a unique constraint real data violates, a type change SQLite's
batch rewrite cannot perform on populated tables.

Two answers here: `test_migrations.py` seeds rows at the *previous* revision using raw SQL
(the ORM would insert today's shape, defeating the point), and staging restores a snapshot
of production before migrating.

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
