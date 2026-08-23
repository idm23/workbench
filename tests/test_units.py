"""The systemd units, checked before they can reach a machine.

A typo in a unit file is invisible until systemd refuses to load it, and by
then it has already been deployed — the automatic deployer rewrites units as
part of a deploy, so a bad template could take the service down without anyone
running a command. The container-based install test cannot catch this either,
because a plain container has no systemd and skips units entirely.

These run wherever pytest does; the `systemd-analyze` check skips itself when
that tool is absent.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from workbench.config import deploy_unit_name, service_name
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


def test_every_unit_is_rendered(rendered):
    assert set(rendered) == {
        f"{SERVICE_NAME}.service",
        f"{DEPLOY_NAME}.service",
        f"{DEPLOY_NAME}.timer",
        f"{RUN_NAME}@.service",
    }


@pytest.mark.parametrize("unit", UNIT_NAMES)
def test_no_placeholder_survives(rendered, unit):
    """An unsubstituted `__REPO__` would be a path systemd cannot resolve."""
    leftover = [line for line in rendered[unit].splitlines() if "__" in line]

    assert leftover == []


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
    """OnUnitActiveSec repeats; Persistent covers a check missed while off."""
    timer = rendered[f"{DEPLOY_NAME}.timer"]

    assert "OnUnitActiveSec=" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


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
