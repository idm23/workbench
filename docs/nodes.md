# Nodes

A **head** runs Workbench. A **node** lends it a GPU. Both come from this repository and
one command each, which is the reproducibility rule applied to more than one machine:
hand someone N boxes and they end up with one head and N-1 nodes, without a conversation.

This is the practical guide. The reasoning behind the split is in `CLAUDE.md` under
*Machines: a head and its nodes*.

## What a node actually is

A machine with a GPU, serving an OpenAI-compatible `/v1/chat/completions` that
`workbench/agents/local.py` drives. It runs:

- **Ollama**, bound to every interface on port 11434, with a model pulled.
- **The deploy timer**, so it updates itself from `main` like everything else.

It does not run the web app, hold a database, or execute runs. Runs happen on the head,
in the head's worktrees; only the model's tokens come from the node.

## Installing one

On a fresh Ubuntu Server machine with an NVIDIA card:

```sh
git clone https://github.com/idm23/workbench.git
cd workbench
./install.sh --role=node
```

That creates the `workbench` account, relocates the checkout to `/srv/workbench`, records
`data/role`, installs Ollama, binds it where the head can reach it, pulls the model, and
turns on the deploy timer. It ends by saying what it could not do for you.

Two things it will not do, both on purpose:

- **Install the GPU driver.** That needs a reboot, which is not a thing an unattended
  script should decide. If `nvidia-smi` is missing you are told to run
  `sudo ubuntu-drivers install`, and to re-run the installer afterwards.
- **Choose the model for you** beyond a default. `WORKBENCH_LOCAL_MODEL` picks it, and
  the default (`qwen2.5-coder:7b`) is what fits in 8 GB of VRAM with room for context.

Re-running `./install.sh --role=node` is safe and is how you pick up a changed drop-in or
a new model.

## Pointing a head at it

Today this is one line, by hand, on the head — in `/etc/workbench/env`:

```
WORKBENCH_AGENT_BACKEND=local
WORKBENCH_INFERENCE_URL=http://192.168.1.153:11434/v1
```

Then `sudo systemctl restart workbench`. Per project, `projects.agent_backend` overrides
the machine-wide default, so one project can use the node while everything else uses
Claude.

**Use the node's LAN address.** Head and node are one hop apart on the same home network,
so that is the direct route; the tailnet works too and is the fallback, not the default.
Nodes registering themselves — so the head learns each one's addresses and probes them in
order — is the next slice of this work.

## Checking it

On the node:

```sh
/srv/workbench/.venv/bin/python -m workbench.doctor
```

which asks the four questions that apply to a node — is this a proper deployment, does
`$HOME` belong to the right account, is there a GPU, and does a model server answer with
the right model loaded.

From the head, the same endpoint the backend will use:

```sh
curl -s http://<node>:11434/v1/models | head
```

And on either machine, what is actually loaded right now:

```sh
ollama ps
journalctl -u ollama -f
```

## When something is wrong

| Symptom | Where to look |
|---|---|
| Runs fail with "No model server answered" | `systemctl status ollama` on the node; check the address in `WORKBENCH_INFERENCE_URL` is the node's LAN one |
| The doctor says the model is not there | `ollama pull <model>` on the node, or set `WORKBENCH_LOCAL_MODEL` to one it has |
| Everything works but is very slow | `nvidia-smi` during a run — if the model is on the CPU, the driver is missing or the model does not fit in VRAM |
| A node stopped updating itself | `systemctl list-timers workbench-deploy.timer`, then `journalctl -u workbench-deploy -n 50` |

## The security note worth reading once

The model server binds `0.0.0.0` and has no authentication, so anything on your home
network can spend that GPU. That is a deliberate trade — the head reaches the node over
the LAN, and `OLLAMA_HOST` takes exactly one address — but it is wider than the tailnet
that the rest of this project assumes. On a network you do not control, put a firewall
rule in front of 11434.
