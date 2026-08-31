"""The checks that stand in for a document nobody reads.

Two things are pinned harder than the rest. That `UNKNOWN` never becomes
`FAIL` — a warning which fires when the checker itself breaks is one people
learn to ignore, and then miss the real one. And that every failure carries
argv rather than advice, because that is the entire difference between this and
the README paragraph it replaces.
"""

import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from workbench import doctor
from workbench.agents.protocol import (
    CREDENTIAL_NONE,
    CREDENTIAL_SUBSCRIPTION,
    CREDENTIAL_UNKNOWN,
    CredentialStatus,
)
from workbench.agents.tests.fake import FakeBackend
from workbench.doctor import CheckState


def answer(stdout: str = "", returncode: int = 0):
    """A stand-in for one probe's completed process."""

    def run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return run


# --- The agent's credential ---------------------------------------------------


def signed_out(monkeypatch, **overrides):
    fields = {
        "backend": "fake",
        "logged_in": False,
        "method": CREDENTIAL_NONE,
        "detail": "Not signed in.",
        "login_command": ("/opt/claude", "auth", "login", "--claudeai"),
    }
    status = CredentialStatus(**{**fields, **overrides})
    monkeypatch.setattr(
        "workbench.agents.registry.get_backend",
        lambda *_a, **_k: FakeBackend(credential=status),
    )
    return status


def test_an_unauthenticated_agent_fails_with_something_you_can_run(monkeypatch):
    signed_out(monkeypatch)

    check = doctor.check_agent_credential()

    assert check.state is CheckState.FAIL
    # argv, not advice. Someone reading this has to be able to paste it.
    assert check.fix == "/opt/claude auth login --claudeai"


def test_an_authenticated_agent_passes_and_offers_no_fix(monkeypatch):
    monkeypatch.setattr("workbench.agents.registry.get_backend", lambda *_a, **_k: FakeBackend())

    check = doctor.check_agent_credential()

    assert check.state is CheckState.OK
    assert check.fix is None


def test_a_credential_that_could_not_be_probed_is_unknown_not_failed(monkeypatch):
    """A missing CLI is not the same as a missing login, and only one of them
    is a problem with this machine."""
    signed_out(monkeypatch, method=CREDENTIAL_UNKNOWN)

    check = doctor.check_agent_credential()

    assert check.state is CheckState.UNKNOWN
    assert not check.failed


def signed_in(monkeypatch, **overrides):
    fields = {
        "backend": "fake",
        "logged_in": True,
        "method": CREDENTIAL_SUBSCRIPTION,
        "account": "someone@example.com",
        "detail": "Signed in as someone@example.com, billing a Claude subscription.",
        "login_command": ("/opt/claude", "auth", "login", "--claudeai"),
    }
    status = CredentialStatus(**{**fields, **overrides})
    monkeypatch.setattr(
        "workbench.agents.registry.get_backend",
        lambda *_a, **_k: FakeBackend(credential=status),
    )
    return status


def test_a_login_about_to_stop_renewing_warns_while_runs_still_work(monkeypatch):
    """The whole point of the check: said days early, on a machine where
    nothing is broken yet and every run still succeeds."""
    signed_in(monkeypatch, renewable_until=datetime.now(UTC) + timedelta(days=2))

    check = doctor.check_agent_credential()

    assert check.state is CheckState.WARN
    # A warning must not set the exit code, or `install.sh` starts failing on a
    # machine that installed perfectly.
    assert not check.failed
    assert "2 days" in check.detail
    # Renewing is what someone would try first, and it is the one thing that
    # does not help — so the detail has to say so where the deadline is said.
    assert "does not extend" in check.detail
    assert check.fix == "/opt/claude auth login --claudeai"


def test_a_login_with_room_left_says_nothing(monkeypatch):
    """A banner that is always up is a banner nobody reads."""
    signed_in(monkeypatch, renewable_until=datetime.now(UTC) + timedelta(days=9))

    check = doctor.check_agent_credential()

    assert check.state is CheckState.OK
    assert check.fix is None


def test_a_backend_that_reports_no_window_is_given_no_deadline(monkeypatch):
    """An API key has no renewal window, and neither does a probe that could
    not read one. Inventing one would be a false alarm on a working machine."""
    signed_in(monkeypatch, renewable_until=None)

    assert doctor.check_agent_credential().state is CheckState.OK


def test_a_login_past_renewing_fails_rather_than_warns(monkeypatch):
    """The backend decides this, not the doctor — a credential that cannot
    renew reports itself signed out, and this is where that lands."""
    signed_out(
        monkeypatch,
        method=CREDENTIAL_SUBSCRIPTION,
        detail="The subscription login expired and can no longer renew itself.",
        renewable_until=datetime.now(UTC) - timedelta(hours=1),
    )

    check = doctor.check_agent_credential()

    assert check.state is CheckState.FAIL
    assert check.fix == "/opt/claude auth login --claudeai"


# --- Tailscale ----------------------------------------------------------------


def test_nothing_published_is_a_warning_even_though_the_probe_succeeded(monkeypatch):
    """`tailscale serve status` answers `{}` and exits 0 with nothing served.

    Reading the exit code instead of the output is the obvious mistake here,
    and it would report a silent machine as healthy.
    """
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(doctor, "_run", answer(stdout="{}"))

    check = doctor.check_tailscale_serve()

    assert check.state is CheckState.WARN
    assert check.fix == "tailscale serve --bg 8787"


def test_this_instances_port_being_published_passes(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/tailscale")
    served = {"Web": {"host:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}}
    monkeypatch.setattr(doctor, "_run", answer(stdout=json.dumps(served)))

    assert doctor.check_tailscale_serve().state is CheckState.OK


def test_the_other_instances_port_does_not_count(monkeypatch):
    """Production and staging share a tailnet and a machine. "Something is
    served" would report staging as published because production is."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/tailscale")
    served = {"Web": {"host:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8788"}}}}}
    monkeypatch.setattr(doctor, "_run", answer(stdout=json.dumps(served)))

    assert doctor.check_tailscale_serve().state is CheckState.WARN


def test_no_tailscale_at_all_is_unknown(monkeypatch):
    """A machine that is not on a tailnet is not a machine that is broken."""
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    check = doctor.check_tailscale_serve()

    assert check.state is CheckState.UNKNOWN
    assert not check.failed


def test_unreadable_serve_output_is_unknown(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(doctor, "_run", answer(stdout="not json"))

    assert doctor.check_tailscale_serve().state is CheckState.UNKNOWN


# --- The deploy key -----------------------------------------------------------


def test_github_authenticating_is_a_pass_despite_exiting_one(monkeypatch, tmp_path):
    """`ssh -T git@github.com` exits 1 on success, because GitHub refuses shell
    access. Reading the code rather than the message inverts this check."""
    key = tmp_path / ".ssh" / "id_ed25519"
    key.parent.mkdir()
    key.touch()
    monkeypatch.setattr(doctor, "running_account", lambda: _account(tmp_path))
    monkeypatch.setattr(
        doctor,
        "_run",
        answer(stdout="Hi idm23/workbench! You've successfully authenticated.", returncode=1),
    )

    assert doctor.check_deploy_key().state is CheckState.OK


def test_a_rejected_key_hands_over_the_public_half_to_paste(monkeypatch, tmp_path):
    """The key itself, not a command that prints it.

    Whoever reads this is about to paste it into a browser, and one of the two
    machines involved is reached over ssh — so telling them to go and run
    something else first, in another window, is a step for no reason.
    """
    key = tmp_path / ".ssh" / "id_ed25519"
    key.parent.mkdir()
    key.touch()
    key.with_name("id_ed25519.pub").write_text("ssh-ed25519 AAAAC3Nz-the-public-half workbench\n")
    monkeypatch.setattr(doctor, "running_account", lambda: _account(tmp_path))
    monkeypatch.setattr(
        doctor, "_run", answer(stdout="Permission denied (publickey).", returncode=255)
    )

    check = doctor.check_deploy_key()

    assert check.state is CheckState.FAIL
    assert "ssh-ed25519 AAAAC3Nz-the-public-half" in check.detail


def test_the_public_key_survives_a_missing_pub_file(tmp_path):
    """Reported rather than raised: a doctor that crashes tells you less than
    one that says it could not tell."""
    assert "could not read" in doctor._public_key(tmp_path / "id_ed25519")


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("git@github.com:idm23/workbench.git", "idm23/workbench"),
        ("https://github.com/idm23/workbench.git", "idm23/workbench"),
        ("https://github.com/idm23/workbench", "idm23/workbench"),
    ],
)
def test_the_paste_link_names_this_repository(monkeypatch, remote, expected):
    """Both remote spellings, because which one a checkout has depends on how
    it was cloned and this only ever builds a URL."""
    monkeypatch.setattr(doctor, "_run", answer(stdout=remote))

    assert doctor._repository_slug() == expected


def test_no_remote_still_produces_a_usable_message(monkeypatch):
    monkeypatch.setattr(doctor, "_run", answer(returncode=128))

    assert doctor._repository_slug() == "<owner>/<repo>"


def test_a_missing_key_names_the_command_that_makes_one(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "running_account", lambda: _account(tmp_path))

    check = doctor.check_deploy_key()

    assert check.state is CheckState.FAIL
    assert check.fix is not None and check.fix.startswith("ssh-keygen")


# --- What the agent has to be able to write -----------------------------------


def test_a_home_the_agent_cannot_write_fails(monkeypatch, tmp_path):
    """The late failure: an OAuth token is refreshed periodically, so a home
    that can be read but not written works for days and then stops."""
    monkeypatch.setattr(doctor, "running_account", lambda: _account(tmp_path))
    monkeypatch.setattr(doctor, "_writable", lambda _path: False)

    check = doctor.check_agent_state()

    assert check.state is CheckState.FAIL
    assert ".claude.json" in check.detail


def test_a_credential_file_that_does_not_exist_yet_is_writable(tmp_path):
    """`.claude.json` is created by the first login, so "missing" must mean
    "writable if the directory is" rather than "broken"."""
    assert doctor._writable(tmp_path / ".claude.json")
    assert not doctor._writable(tmp_path / "nowhere" / ".claude.json")


# --- $HOME --------------------------------------------------------------------


def test_a_borrowed_home_fails_loudly(monkeypatch, tmp_path):
    """`sudo -u workbench` keeps the caller's $HOME while `sudo -iu` does not,
    and every answer below this one is read out of $HOME."""
    monkeypatch.setattr(doctor, "running_account", lambda: _account(tmp_path))
    monkeypatch.setenv("HOME", "/root")

    check = doctor.check_home_directory()

    assert check.state is CheckState.FAIL
    assert check.fix is not None and "sudo -iu" in check.fix


def test_a_matching_home_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "running_account", lambda: _account(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert doctor.check_home_directory().state is CheckState.OK


# --- Running them all ---------------------------------------------------------


def test_a_check_that_crashes_becomes_unknown_rather_than_a_traceback(monkeypatch):
    """A doctor that dies tells you less than one that says it could not tell."""

    def explode():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(doctor, "CHECKS", (explode,))

    results = doctor.run_checks(network=False)

    assert [check.state for check in results] == [CheckState.UNKNOWN]
    assert not results[0].failed


def test_offline_skips_the_only_check_that_needs_a_network(monkeypatch):
    monkeypatch.setattr(doctor, "check_deploy_key", lambda: pytest.fail("the network was reached"))
    monkeypatch.setattr(doctor, "CHECKS", (doctor.check_snapshot_source,))

    assert doctor.run_checks(network=False)


def test_offline_drops_the_network_check_from_the_report(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "running_account", lambda: _account(tmp_path))

    keys = {check.key for check in doctor.run_checks(network=False)}

    assert "deploy-key" not in keys


# --- The command line ---------------------------------------------------------


def all_states(*states: CheckState) -> list[doctor.Check]:
    return [
        doctor.Check(key=f"k{index}", title="t", state=state, detail="d")
        for index, state in enumerate(states)
    ]


def test_warnings_and_unknowns_do_not_fail_the_command(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor, "run_checks", lambda **_k: all_states(CheckState.WARN, CheckState.UNKNOWN)
    )

    assert doctor.main([]) == 0


def test_one_failure_fails_the_command(monkeypatch):
    monkeypatch.setattr(
        doctor, "run_checks", lambda **_k: all_states(CheckState.OK, CheckState.FAIL)
    )

    assert doctor.main([]) == 1


def test_json_mode_writes_one_object_and_nothing_else(monkeypatch, capsys):
    """stdout is the interface here, and the caller parsing it is a web request
    that has no way to ask what went wrong with the stream."""
    monkeypatch.setattr(doctor, "run_checks", lambda **_k: all_states(CheckState.OK))

    doctor.main(["--json"])

    payload = json.loads(capsys.readouterr().out)
    assert [check["state"] for check in payload["checks"]] == ["ok"]


def test_an_unknown_option_is_refused_rather_than_ignored():
    assert doctor.main(["--dry-run"]) == 2


def _account(home):
    """A passwd entry pointing at a temporary directory."""
    import pwd

    real = pwd.getpwuid(0)
    return pwd.struct_passwd(
        (
            real.pw_name,
            real.pw_passwd,
            real.pw_uid,
            real.pw_gid,
            real.pw_gecos,
            str(home),
            real.pw_shell,
        )
    )


def test_a_failure_with_no_single_command_still_reaches_the_to_do_list(caplog):
    """A deploy key is a public half to paste, not a command to run. Filtering
    the list on `fix` quietly dropped it — and a FAIL missing from the list of
    things to do is worse than a slightly longer list."""
    check = doctor.Check(
        key="deploy-key",
        title="The account can push to GitHub",
        state=CheckState.FAIL,
        detail="Add this key: ssh-ed25519 AAAA-paste-me",
    )

    with caplog.at_level("INFO"):
        doctor.report([check])

    assert "ssh-ed25519 AAAA-paste-me" in caplog.text
    assert caplog.text.count("The account can push to GitHub") == 2
