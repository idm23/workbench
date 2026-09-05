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
./install.sh --role=node --head http://homebox-core:8787
```

That creates the `workbench` account, relocates the checkout to `/srv/workbench`, records
`data/role`, installs Ollama, binds it where the head can reach it, pulls the model, and
turns on the deploy timer. It ends by saying what it could not do for you.

Two things it will not do, both on purpose:

- **Install the GPU driver.** That needs a reboot, which is not a thing an unattended
  script should decide. If `nvidia-smi` is missing you are told to run
  `sudo ubuntu-drivers install`, and to re-run the installer afterwards.
- **Choose the model for you** beyond a default. `WORKBENCH_LOCAL_MODEL` picks it, and
  the default (`qwen3:8b`) is what fits in 8 GB of VRAM with room for context *and* can
  actually drive a run — which does not follow from the first. `qwen2.5-coder:7b` is the
  better coder on paper and fails immediately, because it writes its tool calls as prose
  instead of calling them.

  Before trusting a new model with real work, ask it to do one small task:

  ```sh
  uv run scripts/test_local_model.py --model <model> --url http://<node>:11434/v1
  ```

  It reports whether the model used its tools, changed the file, committed, and said what
  it had done — reading the worktree rather than the model's own summary, because those
  disagree more often than you would expect.

  Measured on an RTX 3070 Laptop (8 GB) with 30 GB of system memory:

  | model | weights | result |
  |---|---|---|
  | `qwen2.5-coder:7b` | 4.7 GB | never used the tool channel — cannot drive a run |
  | `qwen3:8b` | 5.2 GB | completed the task, 112s over 8 turns |
  | `gpt-oss:20b` | 13 GB | completed the task, 53s over 10 turns |

  **If your node has 16 GB of memory or more, use `gpt-oss:20b`.** It is twice as quick
  here despite not fitting on the card, because a mixture of experts activates only a
  fraction of itself per token. Put `WORKBENCH_LOCAL_MODEL=gpt-oss:20b` in the node's
  environment before installing, or `ollama pull` it and set the variable on the head.

Re-running `./install.sh --role=node` is safe and is how you pick up a changed drop-in or
a new model.

## Pointing a head at it

The `--head` above is the whole of it. The node POSTs its name, every address it can be
reached on, what it can do, its model and its GPU to `/api/nodes` — at the end of the
install and again on every deploy tick, so an address that changes arrives within five
minutes rather than when someone remembers.

Nothing is typed on the head. It picks a node when a run starts, trying the address that
answered last and then the rest in order, and records which one worked.

One thing still is a decision on the head: whether to use a local model at all.

```
WORKBENCH_AGENT_BACKEND=local
```

in `/etc/workbench/env`, then `sudo systemctl restart workbench`. Per project,
`projects.agent_backend` overrides the machine-wide default, so one project can use the
node while everything else uses Claude. `WORKBENCH_INFERENCE_URL` still works and still
wins when set — it is how you point at a model server that is not a registered node.

**Addresses are offered LAN first.** Head and node are one hop apart on the same home
network, so that is the direct route; the tailnet is the fallback, and the head finds out
which is true by trying rather than by being told.

## Checking it

On the node:

```sh
/srv/workbench/.venv/bin/python -m workbench.doctor
```

which asks the four questions that apply to a node — is this a proper deployment, does
`$HOME` belong to the right account, is there a GPU, and does a model server answer with
the right model loaded.

On the head, the nodes it knows about are listed at `/services`, and the doctor gains a
`A worker node is answering` check whenever this machine is configured for a local model.

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
| A node is missing from `/services` | It was installed without `--head`, or could not reach it — `journalctl -u workbench-deploy -n 50` on the node says which |
| A node's `last seen` is hours old | Its deploy timer has stopped; the node re-registers on every tick, so a stale time means the timer, not the model server |

## The security note worth reading once

The model server binds `0.0.0.0` and has no authentication, so anything on your home
network can spend that GPU. That is a deliberate trade — the head reaches the node over
the LAN, and `OLLAMA_HOST` takes exactly one address — but it is wider than the tailnet
that the rest of this project assumes. On a network you do not control, put a firewall
rule in front of 11434.
