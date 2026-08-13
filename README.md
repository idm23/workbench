# workbench

A personal tool for managing software projects on a home server — projects, a todo tree per
project, and tasks worked either by hand or by a Claude agent, with a written summary either way.

**Nothing is built yet.** What exists today:

| | |
|---|---|
| `CLAUDE.md` | The design doc, including open questions that are not yet resolved. |
| `docs/server-conventions.md` | How the home server launches things, and why. |
| `www/index.html` | A static placeholder page, live on the tailnet. |

Live at **https://homebox-core.tail4c4cf3.ts.net** (tailnet only — not reachable from the
public internet, and not from the laptop, which is on a different tailnet).

## How this is served

There is no application process. `tailscale serve` serves `www/` straight from the checkout on
the server and terminates TLS itself. See `docs/server-conventions.md` for the launch policy that
decides when something gets Docker, systemd, or nothing at all.

### First-time setup on the server

Requires **HTTPS Certificates** enabled in the Tailscale admin console (DNS → HTTPS
Certificates). Verify with `tailscale status --json | grep CertDomains` — an empty value means
it is off, and the phone will get a certificate warning.

```sh
ssh ian@192.168.1.199
git clone https://github.com/idm23/workbench.git ~/workbench
tailscale serve --bg /home/ian/workbench/www
tailscale serve status
```

### Deploying a change

Push from the laptop, pull on the server. `serve` reads from disk, so there is nothing to
restart.

```sh
git push
ssh ian@192.168.1.199 'git -C ~/workbench pull'
```

### Verifying

```sh
# from the server — expect 200 and a valid cert (no -k)
curl -sSI https://homebox-core.tail4c4cf3.ts.net

# what serve is currently mapping
tailscale serve status
```

Then open the URL on the phone. The real acceptance test is **Add to Home Screen** launching
without browser chrome, which is what the valid certificate was for.

## Development

The laptop is on a different tailnet from the server, so development happens over the LAN
(`ian@192.168.1.199`) and tailnet behaviour can only be tested from the phone.

The laptop runs Python 3.10 and the server runs Python 3.14. That gap will matter once there is
application code — the intent is to develop against the server over SSH rather than build locally
and discover the difference at deploy time.
