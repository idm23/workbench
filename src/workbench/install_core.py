"""Install the machine that runs Workbench: the app, the database, the runs.

The head, in the vocabulary `config.role()` uses. This is the install that
existed before there was more than one kind, and it is unchanged in what it
does — it is a separate module now because a second flow arrived, and one
`main()` with a role branch threaded through it is the version nobody can read
half of without holding the other half in their head.

Everything it is made of lives in `workbench.install`; what is here is the
order, which is the part that differs between the two.
"""

import logging
import os

from workbench.config import (
    ROLE_HEAD,
    deploy_branch,
    deploy_unit_name,
    deployment_root,
    host,
    port,
    repo_root,
    service_name,
)
from workbench.install import (
    DEPLOY_INTERVAL,
    InstallError,
    _service_passwd,
    _venv_bin,
    apply_migrations,
    become_root,
    build_environment,
    check_invocation,
    check_prerequisites,
    ensure_agent_identity,
    ensure_agent_state_dir,
    ensure_data_directory,
    ensure_service_account,
    ensure_uv_for_owner,
    hand_off_to,
    info,
    needs_relocation,
    record_role,
    relocate,
    report_outstanding,
    step,
    systemd_is_running,
    wait_for_health,
    warn,
)
from workbench.install import (
    install_service as install_units_and_start,
)
from workbench.logs import BOLD, RED, configure_console_logging, paint

logger = logging.getLogger(__name__)

#: The module systemd, sudo and the relocated checkout all have to come back
#: to. Named once rather than spelled at each re-exec, because the failure when
#: the two disagree is a machine that installs the other role's units.
ENTRY = "workbench.install_core"


def report_success() -> None:
    """What is now true, and what to type next.

    The interpreter named here is the *deployment's*, not `sys.executable`.
    They differ, and the difference is not cosmetic: this process was started
    by `uv run` from whichever checkout someone typed `./install.sh` in, and
    that path survives both the escalation and the handoff to /srv. Printing it
    would tell a person to re-check their install using the abandoned
    checkout's virtualenv — which resolves `repo_root()` to the abandoned
    checkout and then reports, correctly and uselessly, that it is not the
    deployment.
    """
    logger.info("\n%s", paint(BOLD, f"Workbench is running at http://{host()}:{port()}"))
    logger.info(
        "%s",
        f"""
Merges to '{deploy_branch()}' now deploy themselves, within {DEPLOY_INTERVAL}. Nothing
needs running here again — the timer fetches, migrates, and restarts.

    systemctl list-timers {deploy_unit_name()}.timer     # when the next check lands
    sudo systemctl start {deploy_unit_name()}            # deploy right now
    journalctl -u {deploy_unit_name()} -n 50 --no-pager  # what the last one did
    sudo systemctl disable --now {deploy_unit_name()}.timer   # stop deploying

To reach it from a phone over Tailscale (optional, and not automated because
it needs a browser login to your own tailnet):

    sudo tailscale set --operator=$USER   # once, if you have not already
    tailscale serve --bg {port()}

Useful commands:

    {_venv_bin("python")} -m workbench.doctor   # re-check the steps below

    systemctl status {service_name()}
    journalctl -u {service_name()} -f
""",
    )


def main() -> int:
    configure_console_logging()
    os.chdir(repo_root())

    try:
        check_invocation()

        step("Checking prerequisites")
        check_prerequisites()

        # Everything from here creates an account, writes under /srv, or
        # chowns a tree to another user. Asked for once, before any output is
        # captured, so the password prompt is visible rather than a hang.
        become_root(ENTRY)

        if needs_relocation():
            # The account is created only here, because this is the only place
            # anything is given to it. A checkout that is already the
            # deployment has an owner, and that owner *is* the service account
            # by definition — `service_user()` reads it. Creating a second,
            # unused account in that case would leave `User=` and the polkit
            # rule naming one identity while another owned the files.
            step("Preparing the service account")
            account = ensure_service_account()

            step(f"Moving the deployment to {deployment_root()}")
            hand_off_to(relocate(account), ENTRY)
            return 0  # unreachable: hand_off_to execs

        account = _service_passwd()
        info(f"deployment at {repo_root()}, owned by '{account.pw_name}'")

        step("Building the environment")
        build_environment(ensure_uv_for_owner(account))

        step("Preparing the agent's state directory")
        ensure_agent_state_dir(account)

        step("Preparing the account's git identity and SSH key")
        ensure_agent_identity(account)

        step("Preparing the database")
        ensure_data_directory(account)
        record_role(ROLE_HEAD, account)
        apply_migrations()
        info("migrations applied")

        step("Installing the service")
        if not systemd_is_running():
            warn("systemd is not running here (normal inside a container).")
            info("Skipping the service. Start Workbench manually with:")
            info(f"    .venv/bin/uvicorn workbench.app:app --host {host()} --port {port()}")
            logger.info("\n%s\n", paint(BOLD, "Install complete (without the service)."))

            # Still says what a person has to do by hand. Whether systemd is
            # present has no bearing on whether anyone has signed the agent in,
            # and an install that stays silent about it on one path is one that
            # can quietly stop saying it at all — which is what this early
            # return did until the container harness caught it.
            step("Checking what still needs a person")
            report_outstanding()
            return 0

        install_units_and_start()

        step("Waiting for the service to answer")
        wait_for_health()
        report_success()

        step("Checking what still needs a person")
        report_outstanding()
        return 0

    except InstallError as error:
        logger.error("\n%s %s\n", paint(RED, "error:"), error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
