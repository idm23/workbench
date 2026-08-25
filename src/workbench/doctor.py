"""What a person still has to do by hand, and whether they have done it.

Workbench's reproducibility rule says a fresh machine, a clone, and
`./install.sh` produce a running service — and that anything which genuinely
cannot be automated belongs in that script's output rather than in a document.
Two things qualify. Joining a tailnet needs a browser login against an account
no script can know, and so does minting the agent's subscription credential.

This module is the other half of that bargain. The installer names those steps;
this says whether they are done, and it is re-runnable, because the answer
changes long after the install — a credential expires, a `tailscale serve`
mapping is reset, a deploy key is revoked. The failure it exists to prevent is
the one that already happened here: a server that installed and deployed
perfectly, then failed every run at authentication, with nothing anywhere
saying why.

Three rules shape what is below.

**A check that cannot be made is not a failure.** `UNKNOWN` is a distinct state
from `FAIL` and it never sets the exit code, because a warning that fires when
the checker itself breaks is one people learn to ignore — and then miss the
real one.

**Nothing returns early.** A doctor that stopped at the first problem would
hide the second, and these problems arrive together: a machine that has not
been authenticated has usually not had its deploy key added either.

**Every failure carries a command.** `fix` is argv a person can run, not prose
about what they should arrange. That is the difference between this and the
documentation it replaces.
"""

import grp
import json
import logging
import os
import pwd
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from workbench.config import (
    deployment_root,
    instance,
    port,
    repo_root,
    restore_from,
    service_account,
)
from workbench.logs import BOLD, GREEN, RED, YELLOW, configure_console_logging, paint

logger = logging.getLogger(__name__)

#: Short enough that a wedged binary cannot hold a page load or an install
#: open, long enough that a cold start on a loaded box is not a false alarm.
PROBE_TIMEOUT_SECONDS = 20

#: The network probe is a TCP connection to github.com, so it gets its own,
#: larger bound and is skipped entirely by `--offline`.
NETWORK_TIMEOUT_SECONDS = 30

#: Groups that would give the service account root by another name. The whole
#: security claim of a dedicated account is that it has no sudo.
PRIVILEGED_GROUPS = ("sudo", "admin", "wheel", "root")


class CheckState(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Check:
    """One question, its answer, and what to do about it."""

    #: Stable across releases and phrasings, because this is what the web
    #: banner selects on and what a person greps a journal for. The title may
    #: be reworded freely; this may not.
    key: str

    title: str
    state: CheckState
    detail: str

    #: Argv a person can run, joined for display. None when there is nothing
    #: to run — either because the check passed, or because the fix is a
    #: decision rather than a command.
    fix: str | None = None

    @property
    def failed(self) -> bool:
        """Only `FAIL` sets the exit code. See the module docstring."""
        return self.state is CheckState.FAIL


def _run(
    argv: list[str], *, timeout: float = PROBE_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str] | None:
    """Run a probe. Returns None when it could not be run at all.

    Never raises, and never checks the return code — several of the commands
    here answer non-zero while still saying something useful, so the caller
    decides what a failure means.
    """
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        logger.debug("probe %s could not be run: %s", argv[0], error)
        return None


def running_account() -> pwd.struct_passwd:
    """The account this process actually runs as.

    From the effective uid rather than `$USER` or `$HOME`, both of which
    survive a `sudo -u` that did not reset them and would then have this
    reporting confidently on the wrong account's credential.
    """
    return pwd.getpwuid(os.geteuid())


# --- The checks ---------------------------------------------------------------


def check_deployment() -> Check:
    """Whether this checkout is the deployment, owned by the service account.

    Not a failure on a laptop: a developer's checkout is not a deployment and
    never will be. But on the machine that serves it, an owner that is not the
    service account means the units are running as a person.
    """
    title = "The deployment is owned by its own account"
    expected = service_account()
    owner = pwd.getpwuid(repo_root().stat().st_uid).pw_name

    if repo_root() != deployment_root():
        return Check(
            key="deployment",
            title=title,
            state=CheckState.WARN,
            detail=(
                f"Running from {repo_root()}, which is not the deployment at "
                f"{deployment_root()}. Units rendered from here would run as {owner!r}."
            ),
            fix="./install.sh",
        )

    if owner != expected:
        return Check(
            key="deployment",
            title=title,
            state=CheckState.FAIL,
            detail=f"{deployment_root()} is owned by {owner!r}, not by {expected!r}.",
            fix="sudo ./install.sh",
        )

    privileged = sorted(
        group.gr_name
        for group in grp.getgrall()
        if group.gr_name in PRIVILEGED_GROUPS and expected in group.gr_mem
    )
    if privileged:
        return Check(
            key="deployment",
            title=title,
            state=CheckState.FAIL,
            detail=(
                f"{expected!r} is in {', '.join(privileged)}. The account exists to bound what "
                "an agent's shell commands can reach, and membership of any of those undoes it."
            ),
            fix=f"sudo deluser {expected} {privileged[0]}",
        )

    return Check(
        key="deployment",
        title=title,
        state=CheckState.OK,
        detail=f"{deployment_root()} is owned by {expected!r}, which has no sudo.",
    )


def check_home_directory() -> Check:
    """Whether `$HOME` agrees with the account this process runs as.

    Load-bearing rather than pedantic: the backend's credential and its session
    transcripts are found through `$HOME`, so a mismatch makes every answer
    below this one describe some other account. It is also easy to cause —
    `sudo -u workbench` keeps the caller's `$HOME`, while `sudo -iu workbench`
    does not.
    """
    account = running_account()
    title = "$HOME belongs to the account running this"
    home = os.environ.get("HOME")

    if home is None:
        return Check(
            key="home-directory",
            title=title,
            state=CheckState.WARN,
            detail=f"$HOME is unset; {account.pw_dir} would be used.",
            fix=f"sudo -iu {account.pw_name} ...",
        )

    if Path(home) != Path(account.pw_dir):
        return Check(
            key="home-directory",
            title=title,
            state=CheckState.FAIL,
            detail=(
                f"$HOME is {home}, but this runs as {account.pw_name!r} whose home is "
                f"{account.pw_dir}. Everything below would describe the wrong account."
            ),
            fix=f"sudo -iu {account.pw_name} ...",
        )

    return Check(
        key="home-directory",
        title=title,
        state=CheckState.OK,
        detail=f"{account.pw_name!r} at {account.pw_dir}.",
    )


def check_agent_credential() -> Check:
    """Whether an agent could authenticate right now, and on whose account.

    The backend answers this about itself — see `agents/protocol.py`. Nothing
    here knows which vendor is configured, including the command it prints to
    fix it.
    """
    # Imported inside the function so that importing this module never pulls a
    # vendor SDK into whatever process did the importing. The web tier reaches
    # this over a process boundary for the same reason.
    from workbench.agents.registry import UnknownBackend, get_backend

    title = "The agent has a credential"
    backend = get_backend()
    if isinstance(backend, UnknownBackend):
        return Check(
            key="agent-credential",
            title=title,
            state=CheckState.FAIL,
            detail=backend.message,
        )

    status = backend.credential_status()
    fix = " ".join(status.login_command) or None

    if status.logged_in:
        return Check(
            key="agent-credential",
            title=title,
            state=CheckState.OK,
            detail=status.detail,
        )

    state = CheckState.UNKNOWN if status.method == "unknown" else CheckState.FAIL
    return Check(
        key="agent-credential",
        title=title,
        state=state,
        detail=status.detail,
        fix=fix,
    )


def check_agent_state() -> Check:
    """Whether the backend can write the things it has to write.

    This is the failure that arrives late and reads as unrelated. An OAuth
    token is refreshed periodically, so a home the agent can read but not write
    works for days and then stops. `ProtectSystem=strict` makes the whole
    filesystem read-only except an explicit allowlist, so each of these needs a
    `ReadWritePaths` entry in the unit — and `.claude.json` sits *beside* the
    `.claude` directory rather than inside it, which is exactly the sort of
    thing an allowlist misses.
    """
    home = Path(running_account().pw_dir)
    title = "The agent can write its credential and its transcripts"
    unwritable = [
        path
        for path in (home / ".claude", home / ".claude.json", home / ".ssh")
        if not _writable(path)
    ]

    if unwritable:
        return Check(
            key="agent-state",
            title=title,
            state=CheckState.FAIL,
            detail=(
                f"Not writable: {', '.join(str(path) for path in unwritable)}. A refreshed token "
                "that cannot be saved works until the old one expires, then stops."
            ),
            fix="sudo ./install.sh",
        )

    return Check(
        key="agent-state",
        title=title,
        state=CheckState.OK,
        detail=f"{home}/.claude, .claude.json and .ssh are writable.",
    )


def _writable(path: Path) -> bool:
    """Whether this process could write `path`, creating it if absent.

    A missing file is writable if its directory is, which is the case that
    matters: `.claude.json` does not exist until the first login.
    """
    if path.exists():
        return os.access(path, os.W_OK)
    return path.parent.is_dir() and os.access(path.parent, os.W_OK)


def check_git_identity() -> Check:
    """Whether commits this account makes would have an author.

    Agents commit, and the deployer pushes. Neither is interactive, so an unset
    identity is not a prompt — it is a failed run several minutes in.
    """
    title = "The account can author a commit"
    missing = []
    for setting in ("user.name", "user.email"):
        probe = _run(["git", "config", "--global", "--get", setting])
        if probe is None:
            return Check(
                key="git-identity",
                title=title,
                state=CheckState.UNKNOWN,
                detail="git could not be run.",
            )
        if not probe.stdout.strip():
            missing.append(setting)

    if missing:
        return Check(
            key="git-identity",
            title=title,
            state=CheckState.FAIL,
            detail=f"Unset for {running_account().pw_name!r}: {', '.join(missing)}.",
            fix='git config --global user.name "Workbench"',
        )

    return Check(key="git-identity", title=title, state=CheckState.OK, detail="Set.")


def check_deploy_key() -> Check:
    """Whether this account can actually push to GitHub.

    Two traps encoded here. `ssh -T git@github.com` exits **1** on success,
    because GitHub refuses shell access — so this matches the message and
    ignores the code. And an unreachable network is `UNKNOWN`, never `FAIL`:
    being offline is not a misconfiguration.
    """
    home = Path(running_account().pw_dir)
    title = "The account can push to GitHub"
    key = home / ".ssh" / "id_ed25519"

    if not key.exists():
        return Check(
            key="deploy-key",
            title=title,
            state=CheckState.FAIL,
            detail=f"No SSH key at {key}.",
            fix=f'ssh-keygen -t ed25519 -N "" -f {key}',
        )

    probe = _run(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "git@github.com",
        ],
        timeout=NETWORK_TIMEOUT_SECONDS,
    )
    if probe is None:
        return Check(
            key="deploy-key", title=title, state=CheckState.UNKNOWN, detail="ssh could not be run."
        )

    answer = f"{probe.stdout}{probe.stderr}".strip()
    first_line = answer.splitlines()[0] if answer else "no answer"
    if "successfully authenticated" in answer or answer.startswith("Hi "):
        return Check(key="deploy-key", title=title, state=CheckState.OK, detail=first_line)

    return Check(
        key="deploy-key",
        title=title,
        state=CheckState.FAIL,
        detail=f"GitHub did not recognise the key: {first_line}",
        fix=f"cat {key}.pub   # add as a deploy key with write access",
    )


def check_snapshot_source() -> Check:
    """Whether the database this instance restores from is where it says.

    Only staging sets one. It is checked because the path is baked into a unit
    at install time, so it survives the thing it points at moving — and the
    symptom is a deploy that looks fine while quietly restoring nothing.
    """
    title = "The snapshot this instance restores from exists"
    source = restore_from()

    if source is None:
        return Check(
            key="snapshot-source",
            title=title,
            state=CheckState.OK,
            detail="This instance restores from nothing, which is correct for production.",
        )

    if not source.is_file():
        return Check(
            key="snapshot-source",
            title=title,
            state=CheckState.FAIL,
            detail=f"{source} does not exist, so every deploy restores nothing.",
            fix="sudo ./install.sh   # with WORKBENCH_RESTORE_FROM set to the live database",
        )

    if not os.access(source, os.R_OK):
        return Check(
            key="snapshot-source",
            title=title,
            state=CheckState.FAIL,
            detail=f"{source} is not readable by {running_account().pw_name!r}.",
        )

    return Check(
        key="snapshot-source", title=title, state=CheckState.OK, detail=f"Restores from {source}."
    )


def check_tailscale_serve() -> Check:
    """Whether this instance is actually published on the tailnet.

    Valid HTTPS from `tailscale serve` is what makes the phone treat this like
    an app, and it is the second thing the installer cannot do for you. Note
    the empty case answers `{}` and exits **0**, so the verdict comes from the
    output and never from the return code.
    """
    title = "The app is published on the tailnet"
    if shutil.which("tailscale") is None:
        return Check(
            key="tailscale-serve",
            title=title,
            state=CheckState.UNKNOWN,
            detail="tailscale is not installed.",
        )

    probe = _run(["tailscale", "serve", "status", "--json"])
    if probe is None or probe.returncode != 0:
        return Check(
            key="tailscale-serve",
            title=title,
            state=CheckState.UNKNOWN,
            detail="tailscale would not report its serve configuration.",
        )

    try:
        config = json.loads(probe.stdout or "{}")
    except json.JSONDecodeError:
        return Check(
            key="tailscale-serve",
            title=title,
            state=CheckState.UNKNOWN,
            detail="tailscale did not report a readable serve configuration.",
        )

    if _serves_port(config, port()):
        return Check(
            key="tailscale-serve",
            title=title,
            state=CheckState.OK,
            detail=f"Port {port()} is published.",
        )

    return Check(
        key="tailscale-serve",
        title=title,
        state=CheckState.WARN,
        detail=(
            f"Nothing publishes port {port()}, so this is reachable only from the machine itself."
        ),
        fix=f"tailscale serve --bg {port()}",
    )


def _serves_port(config: object, wanted: int) -> bool:
    """Whether any proxy target in the serve config names this instance's port.

    Walks the whole document rather than indexing into it: the shape has
    changed between Tailscale releases, and the question — "does any string in
    here point at localhost:8787" — survives that where a path would not. It
    also keeps production and staging apart, which a bare "is anything served"
    would not.
    """
    needle = f":{wanted}"
    if isinstance(config, str):
        return config.startswith(
            ("http://", "https://", "127.0.0.1", "localhost")
        ) and config.endswith(needle)
    if isinstance(config, dict):
        return any(_serves_port(value, wanted) for value in config.values())
    if isinstance(config, list):
        return any(_serves_port(value, wanted) for value in config)
    return False


#: Every check, in the order a person reads them: what this machine is, then
#: whether the agent can work, then whether the outside world can be reached.
CHECKS = (
    check_deployment,
    check_home_directory,
    check_agent_credential,
    check_agent_state,
    check_git_identity,
    check_snapshot_source,
    check_deploy_key,
    check_tailscale_serve,
)

#: The checks that need to reach the network, skipped by `--offline`.
NETWORK_CHECKS = frozenset({"deploy-key"})


def run_checks(*, network: bool = True) -> list[Check]:
    """Every check, in order. Never raises; a broken check is `UNKNOWN`."""
    results = []
    for check in CHECKS:
        try:
            result = check()
        except Exception:
            # A doctor that crashes tells you less than one that says it could
            # not tell. The traceback still reaches the journal.
            logger.exception("check %s could not be completed", check.__name__)
            results.append(
                Check(
                    key=check.__name__.removeprefix("check_").replace("_", "-"),
                    title=check.__name__,
                    state=CheckState.UNKNOWN,
                    detail="This check could not be completed.",
                )
            )
            continue
        if not network and result.key in NETWORK_CHECKS:
            continue
        results.append(result)
    return results


# --- Reporting ----------------------------------------------------------------

#: How each state is painted and labelled. The label is padded to one width so
#: the details line up down the page, which is what makes a list of eight
#: checks scannable rather than something to read.
_PRESENTATION = {
    CheckState.OK: (GREEN, "ok  "),
    CheckState.WARN: (YELLOW, "warn"),
    CheckState.FAIL: (RED, "FAIL"),
    CheckState.UNKNOWN: (YELLOW, "?   "),
}


def report(checks: list[Check]) -> None:
    """Print every check, then repeat only the ones with something to do.

    The repetition is deliberate. The list above is the state of the machine;
    the list below is a to-do list someone can work through without picking
    the actionable lines back out of it.
    """
    account = running_account().pw_name
    where = instance() or "production"
    logger.info("\n%s", paint(BOLD, f"==> Workbench doctor ({where}, as {account})"))

    for check in checks:
        colour, label = _PRESENTATION[check.state]
        logger.info("    %s  %s", paint(colour, label), check.title)
        logger.info("          %s", check.detail)

    outstanding = [
        check for check in checks if check.state in (CheckState.FAIL, CheckState.WARN) and check.fix
    ]
    if not outstanding:
        return

    logger.info("\n%s", paint(BOLD, "==> What is left to do"))
    for check in outstanding:
        logger.info("    %s", check.title)
        logger.info("        %s", paint(BOLD, check.fix or ""))


def as_payload(checks: list[Check]) -> dict[str, object]:
    """The machine-readable form, read by the web tier over a process boundary."""
    return {
        "instance": instance() or "production",
        "account": running_account().pw_name,
        "checks": [
            {
                "key": check.key,
                "title": check.title,
                "state": check.state.value,
                "detail": check.detail,
                "fix": check.fix,
            }
            for check in checks
        ],
    }


# --- Entry point --------------------------------------------------------------

FLAGS = ("--json", "--offline", "--login")


def _login() -> int:
    """Hand over to whatever signs the configured backend in.

    An `execv` rather than a subprocess: this is an interactive browser login,
    and putting a parent process between a person and a terminal that wants to
    draw a prompt has no upside.
    """
    from workbench.agents.registry import UnknownBackend, get_backend

    backend = get_backend()
    if isinstance(backend, UnknownBackend):
        logger.error("%s %s", paint(RED, "error:"), backend.message)
        return 1

    command = backend.credential_status().login_command
    if not command:
        logger.error(
            "%s The %s backend cannot be signed in from here.", paint(RED, "error:"), backend.name
        )
        return 1

    account = running_account()
    expected = service_account()
    if repo_root() == deployment_root() and account.pw_name != expected:
        # The credential lands in whichever home this runs from, and the unit
        # reads the service account's. Signing in as the wrong user produces a
        # credential nothing will ever use, and a doctor that still says FAIL.
        logger.error(
            "%s This is %s's deployment, but you are %s. Sign in as the account the units run as:\n"
            "       sudo -iu %s %s -m workbench.doctor --login",
            paint(RED, "error:"),
            expected,
            account.pw_name,
            expected,
            sys.executable,
        )
        return 1

    logger.info("Handing over to: %s\n", " ".join(command))
    try:
        os.execv(command[0], list(command))
    except OSError as error:
        logger.error("%s could not run %s: %s", paint(RED, "error:"), command[0], error)
        return 1


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    unknown = [argument for argument in arguments if argument not in FLAGS]
    if unknown:
        configure_console_logging()
        logger.error("%s unknown option %s", paint(RED, "error:"), unknown[0])
        logger.info("usage: python -m workbench.doctor [--json] [--offline] [--login]")
        return 2

    as_json = "--json" in arguments

    if as_json:
        # stdout is the interface in this mode, so logging goes to stderr. A
        # library warning on the wrong stream would corrupt the payload, and
        # the caller parsing it is a web request that cannot ask what happened.
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(message)s")
    else:
        configure_console_logging()

    if "--login" in arguments:
        return _login()

    checks = run_checks(network="--offline" not in arguments)

    if as_json:
        sys.stdout.write(json.dumps(as_payload(checks)))
    else:
        report(checks)

    return 1 if any(check.failed for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
