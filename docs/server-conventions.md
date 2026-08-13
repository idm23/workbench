# Home server conventions

How `homebox-core` runs things, and why. This document is the answer to "how do I launch a new
service on the server" so the question gets settled once instead of per service.

## The machines

| | Server `homebox-core` | Laptop `ian-Katana` |
|---|---|---|
| OS | Ubuntu 26.04 LTS | Ubuntu 22.04.5 LTS |
| Python | 3.14.4 | 3.10.12 |
| git | 2.53.0 | 2.34.1 |
| Node | none | v18.20.8 |
| systemd | 259 | 249 |
| Docker | not installed | — |
| Resources | 14 GB RAM, 860 G free | — |

The server is reachable on the LAN at `ian@192.168.1.199` and on the tailnet as
`homebox-core.tail4c4cf3.ts.net`.

### The two tailnets

The server is on a personal tailnet (two devices: `homebox-core` and an iPhone). The laptop is
logged into a different, shared work tailnet with roughly ten devices, several belonging to other
people.

Two consequences worth remembering:

- **The laptop cannot reach the server over Tailscale.** Development from the laptop happens over
  the LAN. The phone is the real tailnet client, so anything meant to be tested "over the tailnet"
  has to be tested from the phone.
- **Serving without app-level auth is only safe because of which tailnet the server is on.**
  `tailscale serve` publishes to the entire tailnet. On a two-device personal tailnet that is a
  real boundary. If the server were ever joined to the work tailnet, every service here would
  become unauthenticated to a dozen machines we don't control. Do not move the server between
  tailnets without revisiting this.

Never use `tailscale funnel` — that exposes a service to the public internet.

## Launch policy

**Docker Compose for self-contained services. Native systemd units for tools that manipulate the
host.**

Most things fall in the first bucket: media servers, monitoring, databases, anything that takes
input over a port and keeps its state in a volume. Containers give reproducible dependencies, easy
rollback, and sidestep Ubuntu 26.04's PEP 668 restrictions on installing into the system Python.

The exception is software whose actual job is to act on the host. Workbench is the motivating
example: it creates git worktrees, runs project build tools, and executes agent-authored shell
commands inside repo checkouts. Containerizing it would mean:

- Either baking every managed project's toolchain into the image, or bind-mounting repos the
  container has no ability to build.
- Mounting the systemd D-Bus socket, since the deploy path calls
  `systemctl start workbench-deploy.service`.
- Granting Docker socket access, which is root-equivalent on the host and would void the
  "unprivileged service user bounds the blast radius" security model the design depends on.
- Living with UID/GID mismatches between container-written files and host-side git.

Containerizing a host-orchestration tool is like containerizing a backup daemon: you do the work,
then poke enough holes in the isolation that the benefit is gone.

### Deciding for a new service

Ask whether the service needs to see or change things outside its own data:

- **No** — other processes, the host filesystem beyond a volume, systemd, the Docker socket, or
  arbitrary toolchains are all irrelevant to it → **Docker Compose**.
- **Yes** → **systemd unit**, running as a dedicated unprivileged user, with `ReadWritePaths=`
  scoping what it may touch. systemd 259 supports `ProtectSystem=strict` alongside an explicit
  `ReadWritePaths=` allowlist, which is a better middle ground than disabling hardening wholesale.

Docker is not installed yet. It gets installed when the first service that needs it arrives, not
before.

## Static content

Static files need neither Docker nor systemd. `tailscale serve` accepts a file, a directory, text,
or a local server as its target, so a directory of HTML is served directly with no process of our
own:

```sh
tailscale serve --bg /home/ian/workbench/www
```

The config persists in tailscaled state. `tailscale serve status` shows the current mapping and
`tailscale serve reset` clears it.

## Prerequisites in the Tailscale admin console

Two settings are required for a clean phone experience and cannot be set from the CLI:

- **MagicDNS** — enabled.
- **HTTPS Certificates** — required for `tailscale serve` to obtain a valid certificate. Without
  it the phone gets a browser warning instead of a page. Check with
  `tailscale status --json | grep CertDomains`; an empty value means it is off.
