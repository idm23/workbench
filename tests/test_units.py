"""The systemd units, checked before they can reach a machine.

A typo in a unit file is invisible until systemd refuses to load it, and by
then it has already been deployed — the automatic deployer rewrites units as
part of a deploy, so a bad template could take the service down without anyone
running a command. The container-based install test cannot catch this either,
because a plain container has no systemd and skips units entirely.

These run wherever pytest does; the `systemd-analyze` check skips itself when
that tool is absent.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from workbench import install
from workbench.config import (
    agent_git_identity,
    agent_home,
    deploy_unit_name,
    deployment_root,
    service_account,
    service_name,
)
from workbench.install import InstallError, check_not_under_private_tmp, render_unit, units

UNIT_NAMES = [
    "workbench.service",
    "workbench-deploy.service",
    "workbench-deploy.timer",
    "workbench-run@.service",
]

SERVICE_NAME = "workbench"
DEPLOY_NAME = "workbench-deploy"
RUN_NAME = "workbench-run"


@pytest.fixture(autouse=True)
def production_instance(monkeypatch):
    """Default every test to the production instance.

    Without this the suite would inherit whatever WORKBENCH_INSTANCE the shell
    happens to carry, and the assertions about unit names would pass or fail
    depending on the environment that ran them.
    """
    monkeypatch.delenv("WORKBENCH_INSTANCE", raising=False)


@pytest.fixture
def rendered() -> dict[str, str]:
    return {unit: render_unit(template) for unit, template in units()}


def directives(unit: str) -> list[str]:
    """The settings a unit actually declares, without the prose.

    These files carry long comments explaining why they are shaped as they are,
    and some of that prose necessarily names the directives it is arguing
    against. An assertion that a directive is absent has to mean absent from
    the configuration, not unmentioned in the reasoning.
    """
    return [
        line.strip()
        for line in unit.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_every_unit_is_rendered(rendered):
    assert set(rendered) == {
        f"{SERVICE_NAME}.service",
        f"{DEPLOY_NAME}.service",
        f"{DEPLOY_NAME}.timer",
        f"{RUN_NAME}@.service",
    }


@pytest.mark.parametrize("unit", UNIT_NAMES)
def test_no_placeholder_survives(rendered, unit):
    """An unsubstituted `__REPO__` would be a path systemd cannot resolve.

    Directives only. These templates carry comments naming the placeholders
    they argue against — including one explaining why the schedule is no longer
    rendered — and prose that mentions a placeholder is not a placeholder that
    leaked into a setting.
    """
    leftover = [line for line in directives(rendered[unit]) if "__" in line]

    assert leftover == []


def test_no_placeholder_survives_in_the_polkit_rule():
    """The rule is rendered by the same function but is not one of `units()`,
    so it sat outside the check above — which is a bad place for it. An
    unsubstituted `__USER__` there is not a broken path, it is a rule granting
    nothing to nobody, and every run failing at start with an authorisation
    error that names neither the unit nor the account."""
    leftover = [
        line for line in render_unit("workbench-run.rules.template").splitlines() if "__" in line
    ]

    assert leftover == []


@pytest.mark.parametrize("as_instance", ["", "staging"])
def test_the_polkit_rule_grants_the_account_the_unit_runs_as(monkeypatch, as_instance):
    """The agreement three files have to reach without ever seeing each other.

    systemd reads `User=` from the unit; polkit matches `subject.user` against
    the uid that asks. If those two names diverge the service starts perfectly
    and every run dies at the moment it is started, with an error that mentions
    neither file. Nothing else in the suite would catch it.
    """
    monkeypatch.setenv("WORKBENCH_INSTANCE", as_instance)

    rule = render_unit("workbench-run.rules.template")
    service = render_unit("workbench-run@.service.template")

    granted = re.search(r'subject\.user !== "([^"]+)"', rule)
    runs_as = re.search(r"^User=(.+)$", service, re.MULTILINE)

    assert granted is not None and runs_as is not None
    assert granted.group(1) == runs_as.group(1)


def test_the_polkit_rule_grants_only_this_instances_units(staging):
    """Production and staging grant different unit patterns. A rule that
    matched the other instance's would let staging start production's runs."""
    monkeypatch_free_rule = render_unit("workbench-run.rules.template")

    assert "workbench-staging-run" in monkeypatch_free_rule
    assert "workbench-run@" not in monkeypatch_free_rule


def test_the_app_runs_unprivileged(rendered):
    """The whole security posture rests on this one line."""
    unit = rendered[f"{SERVICE_NAME}.service"]

    assert "User=root" not in unit
    assert any(line.startswith("User=") for line in unit.splitlines())


def test_the_deployer_runs_as_root(rendered):
    """It restarts the service, which an unprivileged user cannot do."""
    assert "User=root" in rendered[f"{DEPLOY_NAME}.service"]


def test_the_deployer_is_a_oneshot(rendered):
    """A long-running Type would make the timer refuse to fire it again."""
    assert "Type=oneshot" in rendered[f"{DEPLOY_NAME}.service"]


def test_the_deployer_is_not_the_app(rendered):
    """Separate units mean the restart lands on a different cgroup.

    If the deployer shared a unit with the app, restarting the app would kill
    the process doing the restarting — the self-deployment trap.
    """
    app = rendered[f"{SERVICE_NAME}.service"]
    deployer = rendered[f"{DEPLOY_NAME}.service"]

    assert "uvicorn" in app
    assert "uvicorn" not in deployer
    assert "workbench.deploy" in deployer
    assert "workbench.deploy" not in app


def test_the_timer_repeats_and_catches_up(rendered):
    """A calendar schedule, which repeats, and Persistent, which now works.

    `Persistent=` only applies to `OnCalendar=` timers. It was in this file
    for weeks alongside a monotonic schedule, documented as catching up a
    check missed while the machine was off, and doing nothing at all.
    """
    timer = rendered[f"{DEPLOY_NAME}.timer"]

    assert "OnCalendar=" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_the_timer_survives_being_restarted(rendered):
    """The property the old assertion was blind to, having asserted the bug.

    A monotonic timer measures from an anchor — the boot, or the last time the
    service it triggers ran. Restart it any later than boot and both anchors
    are gone: the boot moment cannot recur, and the interval has nothing to
    measure from until the service runs, which is what the timer was meant to
    cause. systemd parks it at `active (elapsed)`, reporting itself enabled and
    active, and never fires again until reboot.

    That is not hypothetical: `install_units` restarts this timer on purpose
    whenever the rendered file changes, so the one path meant to update the
    schedule was the path that turned deployment off. A calendar schedule has
    no anchor to lose.
    """
    settings = directives(rendered[f"{DEPLOY_NAME}.timer"])

    assert not [line for line in settings if line.startswith(("OnBootSec=", "OnUnitActiveSec="))]
    assert any(line.startswith("OnCalendar=") for line in settings)


def test_the_schedule_is_one_systemd_accepts(rendered):
    """A calendar expression is a small language, and a typo in it does not
    fail loudly — it produces a timer that simply never fires."""
    schedule = next(
        line.split("=", 1)[1]
        for line in directives(rendered[f"{DEPLOY_NAME}.timer"])
        if line.startswith("OnCalendar=")
    )

    checked = subprocess.run(
        ["systemd-analyze", "calendar", schedule],
        capture_output=True,
        text=True,
        check=False,
    )
    if checked.returncode != 0 and "command not found" in checked.stderr:
        pytest.skip("systemd-analyze is not available here")

    assert checked.returncode == 0, checked.stderr
    # It has to actually recur. `systemd-analyze` accepts a one-shot timestamp
    # perfectly happily, and that would deploy once and never again.
    assert "Next elapse:" in checked.stdout


def test_the_timer_names_the_service_it_triggers():
    """systemd pairs a timer with the service of the same stem, so the names
    have to stay in lockstep even though nothing references them explicitly."""
    timer = f"{DEPLOY_NAME}.timer"
    service = f"{DEPLOY_NAME}.service"

    assert timer.removesuffix(".timer") == service.removesuffix(".service")
    assert {timer, service} <= set(UNIT_NAMES)


def test_no_secret_is_baked_into_a_unit(rendered):
    """Credentials belong in EnvironmentFile, which is not in the repository.

    Scans directives only. Comments are allowed to *mention* tokens — they have
    to, to explain where the real one lives — so matching on prose would make
    this fail for documenting itself.
    """
    secretish = ("token", "password", "api_key", "secret")

    for unit, text in rendered.items():
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not stripped.startswith("Environment="):
                continue

            _, _, assignment = stripped.partition("Environment=")
            name, _, value = assignment.partition("=")
            if any(word in name.lower() for word in secretish):
                assert value == "", f"{unit} assigns {name} a literal value"


# --- Two instances on one machine --------------------------------------------
#
# Staging is a second install on the same box. Almost everything that separates
# it comes free from being a second checkout — its own data/, venv, and port —
# but the unit names do not, and a collision would mean the staging deployer
# restarting production on every merge to `staging`.


@pytest.fixture
def staging(monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("WORKBENCH_INSTANCE", "staging")
    monkeypatch.setenv("WORKBENCH_PORT", "8788")
    monkeypatch.setenv("WORKBENCH_DEPLOY_BRANCH", "staging")
    return {unit: render_unit(template) for unit, template in units()}


def test_staging_units_are_named_apart_from_production(staging):
    assert set(staging) == {
        "workbench-staging.service",
        "workbench-staging-deploy.service",
        "workbench-staging-deploy.timer",
        "workbench-staging-run@.service",
    }


def test_no_unit_name_is_shared_between_instances(rendered, staging):
    """The collision that would have staging restarting production."""
    assert set(rendered).isdisjoint(set(staging))


def test_instances_do_not_share_a_port(rendered, staging):
    assert "--port 8787" in rendered["workbench.service"]
    assert "--port 8788" in staging["workbench-staging.service"]


def test_each_deployer_manages_its_own_instance(staging):
    """A deployer that resolved the default names would restart the wrong app."""
    deployer = staging["workbench-staging-deploy.service"]

    assert "WORKBENCH_INSTANCE=staging" in deployer
    assert "WORKBENCH_DEPLOY_BRANCH=staging" in deployer


def test_production_restores_from_nothing(rendered, monkeypatch):
    """Restoring is a staging-only affordance.

    A production deployer that restored a snapshot would be overwriting live
    data on every deploy, so the rendered value must be empty even when the
    environment happens to carry one.
    """
    monkeypatch.setenv("WORKBENCH_RESTORE_FROM", "")
    deployer = render_unit("workbench-deploy.service.template")

    assigned = [
        line.strip()
        for line in deployer.splitlines()
        if line.strip().startswith("Environment=WORKBENCH_RESTORE_FROM=")
    ]

    assert assigned == ["Environment=WORKBENCH_RESTORE_FROM="]


def test_staging_restores_from_the_configured_database(monkeypatch):
    monkeypatch.setenv("WORKBENCH_INSTANCE", "staging")
    monkeypatch.setenv("WORKBENCH_RESTORE_FROM", "/home/ian/workbench/data/workbench.db")

    deployer = render_unit("workbench-deploy.service.template")

    assert "WORKBENCH_RESTORE_FROM=/home/ian/workbench/data/workbench.db" in deployer


def test_instance_names_are_derived_consistently(monkeypatch):
    monkeypatch.setenv("WORKBENCH_INSTANCE", "staging")

    assert service_name() == "workbench-staging"
    assert deploy_unit_name() == "workbench-staging-deploy"


# --- Where a deployment lives, and who owns it --------------------------------
#
# The unit name, the directory under /srv, and the account are deliberately one
# rule with one spelling. They are read by three things that never see each
# other — the unit's `User=`, the polkit rule's `subject.user`, and the
# filesystem — and a disagreement between any two of them is not a visible bug.
# It is every run failing to start with an authorisation error naming neither.


def test_the_deployment_lives_outside_anybodys_home():
    """A checkout under /home cannot be reached by the account that serves it.

    Ubuntu creates home directories mode 0750, so this is not a preference:
    the service could not traverse into a checkout under one.
    """
    assert deployment_root() == Path("/srv/workbench")
    assert not deployment_root().is_relative_to(Path("/home"))


def test_the_account_is_the_service_name():
    """The invariant the whole scheme rests on. If this can drift, so can the
    unit and the polkit rule that are rendered from it."""
    assert service_account() == service_name() == "workbench"


def test_staging_gets_its_own_account_and_directory(staging):
    """Staging restores production's database every deploy and runs the same
    agent code. Sharing an account would leave a staging agent holding write
    access to production's checkout, worktrees, and database."""
    assert service_account() == "workbench-staging"
    assert deployment_root() == Path("/srv/workbench-staging")
    assert agent_home() == Path("/home/workbench-staging")


def test_the_instances_share_nothing(staging):
    production = Path("/srv/workbench")
    assert deployment_root() != production
    assert not deployment_root().is_relative_to(production)
    assert service_account() != SERVICE_NAME


def test_an_account_name_fits_in_a_username(staging):
    """32 characters is the Linux limit, and `useradd` fails past it."""
    assert len(service_account()) <= 32


def test_a_developers_checkout_can_say_it_is_not_a_deployment(monkeypatch, tmp_path):
    """A laptop and both test harnesses run the code from wherever it was
    cloned. Without this they would all be told to relocate to /srv."""
    monkeypatch.setenv("WORKBENCH_DEPLOYMENT_ROOT", str(tmp_path))

    assert deployment_root() == tmp_path.resolve()


def test_the_agent_home_is_not_inside_the_checkout():
    """The credential and the session transcripts are the two things here that
    are on neither GitHub nor the database. A relocation re-copies the
    checkout; these have to survive that."""
    assert agent_home() == Path("/home/workbench")
    assert not agent_home().is_relative_to(deployment_root())


def test_the_account_commits_under_a_name_that_identifies_the_machine():
    """git refuses to commit without an identity, and nothing here can answer a
    prompt — so the failure is an agent run dying several minutes in, having
    already done the work. The default email is non-routable on purpose: it
    says which machine made the commit without inventing a mailbox."""
    name, email = agent_git_identity()

    assert name == "Workbench"
    assert email.startswith("workbench@")


def test_the_identity_can_be_pointed_at_a_real_account(monkeypatch):
    """For when commits should be attributed to a GitHub user instead."""
    monkeypatch.setenv("WORKBENCH_GIT_NAME", "Ian's Robot")
    monkeypatch.setenv("WORKBENCH_GIT_EMAIL", "robot@example.com")

    assert agent_git_identity() == ("Ian's Robot", "robot@example.com")


# --- PrivateTmp ---------------------------------------------------------------
#
# The unit gets its own /tmp and /var/tmp. A checkout under either is invisible
# from inside the service, and systemd's only complaint is that "the control
# process exited with error code" against an empty journal. Caught once in CI;
# refused up front from now on.


def test_a_checkout_under_tmp_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr("workbench.install.repo_root", lambda: Path("/tmp/workbench"))

    with pytest.raises(InstallError, match="PrivateTmp"):
        check_not_under_private_tmp()


def test_var_tmp_is_refused_too(monkeypatch):
    """PrivateTmp covers /var/tmp as well, which is the less obvious half."""
    monkeypatch.setattr("workbench.install.repo_root", lambda: Path("/var/tmp/workbench"))

    with pytest.raises(InstallError):
        check_not_under_private_tmp()


def test_a_checkout_under_home_is_fine(monkeypatch):
    monkeypatch.setattr("workbench.install.repo_root", lambda: Path.home() / "workbench")

    check_not_under_private_tmp()


def test_the_service_still_gets_a_private_tmp(rendered):
    """The refusal above only makes sense while this is actually set."""
    assert "PrivateTmp=yes" in rendered["workbench.service"]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not present")
def test_systemd_accepts_the_units(rendered, tmp_path):
    """The real parser's opinion, rather than ours.

    Catches unknown directives and malformed values that string assertions
    above would happily let through.
    """
    for unit, text in rendered.items():
        (tmp_path / unit).write_text(text)

    result = subprocess.run(
        ["systemd-analyze", "verify", *(str(tmp_path / unit) for unit in rendered)],
        capture_output=True,
        text=True,
        check=False,
    )

    # Complaints about *our* units name them; systemd also emits unrelated
    # warnings about whatever is installed on the host, which are not ours.
    ours = [
        line
        for line in (result.stderr + result.stdout).splitlines()
        if any(unit in line for unit in rendered)
    ]

    assert ours == [], "\n".join(ours)


def test_a_deploy_installs_everything_an_install_does(monkeypatch):
    """The gap that made every run fail with access denied on an updated box.

    `install.sh` wrote the polkit rule and the deployer did not, so a machine
    that only ever updated automatically got the run unit without the
    authorisation to start it. The fix for that would have been a remembered
    manual step, which is the thing the automatic deployer exists to abolish.
    """
    from workbench import deploy

    called: list[str] = []
    monkeypatch.setattr("workbench.install.systemd_is_running", lambda: True)
    monkeypatch.setattr("workbench.install.install_units", lambda: called.append("units"))
    monkeypatch.setattr("workbench.install.install_polkit_rule", lambda: called.append("polkit"))

    assert deploy.refresh_units() is None
    assert called == ["units", "polkit"]


def test_a_failure_installing_the_rule_is_reported_not_raised(monkeypatch):
    """A deploy reports into the journal rather than dying on a traceback."""
    from workbench import deploy

    monkeypatch.setattr("workbench.install.systemd_is_running", lambda: True)
    monkeypatch.setattr("workbench.install.install_units", lambda: None)

    def explode():
        raise OSError("read-only filesystem")

    monkeypatch.setattr("workbench.install.install_polkit_rule", explode)

    result = deploy.refresh_units()

    assert result is not None
    assert "read-only filesystem" in str(result)


def test_units_are_installed_even_when_there_is_nothing_to_pull(monkeypatch):
    """The trap that left a machine running code whose install half never ran.

    A change to the deployer takes effect on the deploy *after* the one that
    delivered it — the process imported its own code before pulling — so the
    run that brings in a new install step is the last run that does not perform
    it. Without converging on an idle tick, that step then waits for an
    unrelated commit.
    """
    from workbench import deploy

    called: list[str] = []
    monkeypatch.setattr(deploy, "advance_checkout", lambda: deploy.AlreadyCurrent("abc1234"))
    monkeypatch.setattr(deploy, "refresh_units", lambda: called.append("refreshed"))

    result = deploy.deploy()

    assert isinstance(result, deploy.AlreadyCurrent)
    assert called == ["refreshed"]


def test_an_idle_tick_does_not_restart_anything(monkeypatch):
    """Converging is not deploying: nothing was pulled, so nothing is rebuilt."""
    from workbench import deploy

    monkeypatch.setattr(deploy, "advance_checkout", lambda: deploy.AlreadyCurrent("abc1234"))
    monkeypatch.setattr(deploy, "refresh_units", lambda: None)
    monkeypatch.setattr(
        deploy, "rebuild_and_restart", lambda: pytest.fail("should not rebuild on an idle tick")
    )

    assert isinstance(deploy.deploy(), deploy.AlreadyCurrent)


def test_a_failure_converging_is_reported(monkeypatch):
    from workbench import deploy

    monkeypatch.setattr(deploy, "advance_checkout", lambda: deploy.AlreadyCurrent("abc1234"))
    monkeypatch.setattr(
        deploy, "refresh_units", lambda: deploy.DeployFailed("installing systemd units", "nope")
    )

    assert isinstance(deploy.deploy(), deploy.DeployFailed)


def test_the_install_points_at_the_deployments_own_interpreter(monkeypatch, tmp_path, caplog):
    """Not `sys.executable`, which is a different thing after a relocation.

    The installer is started by `uv run` from whichever checkout someone typed
    `./install.sh` in, and that interpreter path survives both the escalation
    and the handoff to /srv. Printing it tells a person to re-check their
    install using the *abandoned* checkout's virtualenv, which then reports —
    correctly and uselessly — that it is not the deployment.
    """
    monkeypatch.setattr("workbench.install.repo_root", lambda: tmp_path)

    with caplog.at_level("INFO"):
        install.report_success()

    assert f"{tmp_path}/.venv/bin/python -m workbench.doctor" in caplog.text
    assert sys.executable not in caplog.text


def test_every_timer_placeholder_is_one_an_older_installer_provides():
    """A template is read from the new checkout and rendered by the old
    installer — the deployer imports its own code before it pulls. So a
    template needing a placeholder the running renderer has never heard of
    emits it verbatim, and `OnCalendar=__SCHEDULE__` is a bad unit file setting
    that fails the timer restart and kills the timer.

    Adding a placeholder is safe, because the old renderer simply never
    encounters it. Requiring a *new* one in an existing template is not. This
    pins the timer's schedule as literal text for that reason.
    """
    template = (Path("deploy") / "workbench-deploy.timer.template").read_text()
    schedule = [line for line in template.splitlines() if line.startswith("OnCalendar=")]

    assert schedule == ["OnCalendar=*:0/5"]
    assert "__" not in schedule[0]


def test_the_interval_a_person_is_told_matches_the_one_configured():
    """The schedule moved into the template and the wording stayed behind, so
    nothing but this keeps them honest. Being told deploys land within five
    minutes when they land every fifteen is worse than being told nothing."""
    schedule = next(
        line.split("=", 1)[1]
        for line in directives(render_unit("workbench-deploy.timer.template"))
        if line.startswith("OnCalendar=")
    )

    minutes = schedule.rsplit("/", 1)[-1]
    assert f"{minutes}min" == install.DEPLOY_INTERVAL
