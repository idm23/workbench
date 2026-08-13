#!/bin/sh
#
# Bootstrap for Workbench.
#
#   ./install.sh
#
# This script exists only to get `uv` onto the machine. Everything else lives
# in src/workbench/install.py, which is Python: testable, and able to import
# the application's own configuration instead of duplicating the port and
# database path here where they could drift.
#
# Safe to re-run.

set -eu

cd "$(dirname "$0")"

# The uv installer writes to ~/.local/bin, which is frequently not on PATH in a
# non-login shell. This has to come before the check below, or a re-run finds
# no uv and downloads it again every time.
PATH="$HOME/.local/bin:$PATH"
export PATH

if ! command -v uv >/dev/null 2>&1; then
    printf 'Installing uv...\n'
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi

if ! command -v uv >/dev/null 2>&1; then
    printf 'uv was installed but is not on PATH (%s/.local/bin)\n' "$HOME" >&2
    exit 1
fi

# `uv run` builds the virtualenv from uv.lock before running, so the installer
# starts with its own dependencies already present.
#
# --no-dev matters: the dev group carries pyright, which pulls nodeenv and
# downloads a Node runtime on first use. A deployed machine has no reason to
# install a linter, and this project deliberately keeps Node off the server.
exec uv run --frozen --no-dev python -m workbench.install "$@"
