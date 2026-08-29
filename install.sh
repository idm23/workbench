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

# --no-project, so this does NOT build a virtualenv first. The installer is
# written against the standard library alone precisely so it can run before one
# exists — because one of the first things it decides is whether this checkout
# is where the deployment belongs, and building a few hundred megabytes of
# environment in a directory it is about to abandon is worth avoiding.
#
# The real environment is built later, at the deployment, by the account that
# will own it. --no-dev applies there: the dev group carries pyright, which
# pulls nodeenv and downloads a Node runtime on first use, and this project
# deliberately keeps Node off the server.
#
# PYTHONPATH rather than an install, for the same reason: nothing is installed
# yet, and the installer has to import workbench.config to know where anything
# goes.
PYTHONPATH="$(pwd)/src"
export PYTHONPATH

exec uv run --no-project --python "$(cat .python-version)" python -m workbench.install "$@"
