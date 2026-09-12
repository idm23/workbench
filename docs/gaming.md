# Gaming nodes

A node can also stream a game to a television, on the same GPU it lends to inference. This
is the practical guide. The reasoning behind the split — a switch rather than a shared
scheduler, why the render surface is a swappable decision rather than a settled one — is in
`CLAUDE.md` under *Machines: a head and its nodes*.

## What a gaming node adds

Everything a plain node already is (see `docs/nodes.md`), plus:

- **The switch** — `workbench-gaming.service`, which stops Ollama and re-registers the node
  as not offering inference the moment a game needs the card back, and reverses both when
  the game ends. Driven by Sunshine's `global_prep_cmd`, not by a person.
- **A render surface** — Steam and Sunshine need something to capture from, and a node has
  no monitor. `x11-dummy` is the default: a virtual X11 display plus NvFBC capture, chosen
  for being the deeper community precedent specifically for Sunshine on NVIDIA, not because
  it is the newer or lighter option. See `render.py`'s own docstring before touching this.
- **Steam and Sunshine themselves.**

## Installing one

```sh
sudo ./install.sh --role=node --capabilities=inference,gaming --gaming-user=<the person>
```

`--gaming-user` is not optional in practice: it is whose graphical session Sunshine and
Steam run in, whose groups get Sunshine's capture permissions, and the one account the
polkit rule names. Defaults to `$SUDO_USER`, which is right whenever the person running the
installer is the person who will play — wrong, and silently so, in a container or
already-root shell, which is why it is required rather than guessed there.

Re-running the same command is safe. It is also how you turn a plain node into a gaming
one after the fact, or pick up a changed template.

## The render surface, and what running it found

- **`workbench-x11.service` is a system unit, root, and that is load-bearing rather than a
  narrow-privilege afterthought.** A first attempt ran the X server as a `systemd --user`
  unit for the gaming account, on the theory that Steam and Sunshine's own privilege
  narrowing should extend to the display too. It cannot work, independent of any one
  machine's configuration: a user unit always runs as whoever's own systemd instance loaded
  it, so it can never be root — and opening the VT this needs is root-only on a machine with
  no setuid `Xorg.wrap` (confirmed on this project's own node: `/dev/tty7` is `crw-------`,
  zero permission bits for anyone else, `tty`-group membership included). Steam and Sunshine
  are unaffected — they are X *clients*, connecting to `:0`, and stay on the gaming account.
- **The Xorg `ConnectedMonitor` / `CustomEDID` trick has run against real hardware and
  worked** on the first try, once the unit itself was fixed. `python -m workbench.doctor`'s
  render-surface check is a real probe now, not a permanent `unknown`.
- **A cheap HDMI dummy plug (~$5-10) is still worth knowing about** as a fallback, per an
  explicit decision to try the software-only virtual display first — it gives a real EDID
  instead of a synthetic one, if a different card ever needs it.
- **`gamescope`** is named in `config.RENDER_BACKENDS` and stubbed in `render.py`, but not
  implemented — NVIDIA's headless-Wayland output story has historically been the
  less-exercised path on that vendor's driver, which is why `x11-dummy` was tried first, not
  a reason to expect `gamescope` to fail if it is ever built.

## Pairing

Sunshine's pairing is a one-time browser action, the same shape as joining a tailnet or
signing the agent in — not something `install.sh` can do for you. Visit
`https://<the node>:47990` and pair whatever will stream from it (a Moonlight client, for
instance). `python -m workbench.doctor` reports whether this has happened, but — see the
note in `doctor.check_sunshine_paired` — cannot yet read Sunshine's actual paired-client
state, so it reports `unknown` rather than guessing until that is confirmed on real
hardware.

## Checking it

```sh
/srv/workbench/.venv/bin/python -m workbench.doctor
```

adds four questions on top of a plain node's: is Steam installed, is Sunshine installed, is
a render surface actually up, and has anything paired with Sunshine. None of these fail the
doctor's exit code — a gaming node missing Steam still lends its GPU to inference exactly
as well as one that was never asked to game.

## When something is wrong

| Symptom | Where to look |
|---|---|
| The doctor says Steam or Sunshine is missing | Re-run the install command above |
| A stream connects but shows nothing | `systemctl status workbench-x11` on the node — a system unit, so no `--user`/`-M` needed; no render surface means nothing to capture |
| Sunshine's prep command does not flip the switch | `systemctl status workbench-gaming` on the node (also a system unit); confirm the polkit rule was granted (`ls /etc/polkit-1/rules.d/`) |
| The switch flips but inference never comes back | `journalctl -u ollama`; `python -m workbench.gaming stop` by hand reports the same thing Sunshine's `undo` command would |
| Steam or Sunshine cannot reach the GPU | `groups <gaming user>` — should include `video` and `input`; re-run the install command, which grants these idempotently |
