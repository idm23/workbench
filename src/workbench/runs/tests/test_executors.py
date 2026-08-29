"""Where a run executes.

The systemd executor is tested against a stubbed `systemctl` rather than a real
one. What is worth pinning is the *shape* of what it asks for — the unit name,
`--no-block` so a web request is not held open for the length of a run, and
which unit states count as alive — none of which needs a real init system, and
all of which would be silently wrong on a machine that has one.
"""

import os
import subprocess

import pytest

from workbench.config import run_unit_name
from workbench.runs import executors as executors_module
from workbench.runs.executors import (
    DEFAULT_EXECUTOR,
    SYSTEMD_EXECUTOR,
    Executor,
    LocalProcessExecutor,
    Started,
    StartRefused,
    SystemdUnitExecutor,
    UnknownExecutor,
    available_executors,
    get_executor,
)


@pytest.fixture
def systemctl(monkeypatch):
    """Record every systemctl invocation, and script its replies."""
    calls: list[list[str]] = []
    replies: dict[str, tuple[int, str]] = {}

    def fake_run(argv, **_):
        calls.append(list(argv))
        code, out = replies.get(argv[1], (0, ""))
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    monkeypatch.setattr(executors_module.subprocess, "run", fake_run)
    return calls, replies


# --- The systemd executor --------------------------------------------------


def test_a_run_is_started_as_its_own_unit(systemctl):
    calls, _ = systemctl

    result = SystemdUnitExecutor().start(42)

    assert isinstance(result, Started)
    assert result.handle == run_unit_name(42)
    assert calls[0] == ["systemctl", "start", "--no-block", run_unit_name(42)]


def test_starting_does_not_wait_for_the_run_to_finish(systemctl):
    """A run lasts minutes and this is called from a web request."""
    calls, _ = systemctl

    SystemdUnitExecutor().start(1)

    assert "--no-block" in calls[0]


def test_a_refused_start_explains_itself(systemctl):
    """The likely cause is the polkit rule, and the message is the only clue."""
    _, replies = systemctl
    replies["start"] = (1, "Access denied")

    result = SystemdUnitExecutor().start(7)

    assert isinstance(result, StartRefused)
    assert "Access denied" in result.message


def test_no_systemd_at_all_is_a_refusal_not_a_crash(monkeypatch):
    def missing(*_, **__):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(executors_module.subprocess, "run", missing)

    assert isinstance(SystemdUnitExecutor().start(1), StartRefused)


def test_cancelling_stops_the_unit(systemctl):
    calls, _ = systemctl

    assert SystemdUnitExecutor().cancel("workbench-run@3.service") is True
    assert calls[0][:2] == ["systemctl", "stop"]


@pytest.mark.parametrize("state", ["active", "activating", "reloading"])
def test_a_unit_on_its_way_up_counts_as_running(systemctl, state):
    """Treating `activating` as dead would reap a run at the moment it started."""
    _, replies = systemctl
    replies["is-active"] = (0, state)

    assert SystemdUnitExecutor().is_running("workbench-run@1.service") is True


@pytest.mark.parametrize("state", ["inactive", "failed", "deactivating", ""])
def test_anything_else_counts_as_gone(systemctl, state):
    _, replies = systemctl
    replies["is-active"] = (3, state)

    assert SystemdUnitExecutor().is_running("workbench-run@1.service") is False


def test_a_wedged_systemctl_does_not_hang_the_caller(monkeypatch):
    def times_out(*_, **__):
        raise subprocess.TimeoutExpired("systemctl", 20)

    monkeypatch.setattr(executors_module.subprocess, "run", times_out)

    assert SystemdUnitExecutor().is_running("anything") is False


# --- The local fallback ----------------------------------------------------


def test_a_local_run_is_a_real_detached_process(tmp_path, monkeypatch):
    """Verified against a process that exists, not a mocked one."""
    started: dict[str, list[str] | bool | None] = {}

    # Captured before patching: `executors_module.subprocess` is the module
    # object itself, so calling `subprocess.Popen` inside the fake would call
    # the fake.
    real_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):
        started["argv"] = list(argv)
        started["session"] = kwargs.get("start_new_session")
        return real_popen(["sleep", "5"], start_new_session=True)

    monkeypatch.setattr(executors_module.subprocess, "Popen", fake_popen)
    executor = LocalProcessExecutor()

    result = executor.start(9)

    assert isinstance(result, Started)
    assert started["session"] is True
    assert started["argv"][1:] == ["-m", "workbench.runs.runner", "9"]  # type: ignore[index]
    assert executor.is_running(result.handle) is True
    executor.cancel(result.handle)


def test_a_pid_that_is_gone_is_not_running():
    executor = LocalProcessExecutor()
    process = subprocess.Popen(["true"])
    process.wait()

    assert executor.is_running(str(process.pid)) is False


def test_our_own_process_is_running():
    assert LocalProcessExecutor().is_running(str(os.getpid())) is True


def test_a_handle_that_is_not_a_pid_is_handled(caplog):
    """A row written by another executor should not crash this one."""
    executor = LocalProcessExecutor()

    assert executor.is_running("workbench-run@1.service") is False
    assert executor.cancel("workbench-run@1.service") is False


def test_cancelling_something_already_dead_reports_failure():
    process = subprocess.Popen(["true"])
    process.wait()

    assert LocalProcessExecutor().cancel(str(process.pid)) is False


# --- Resolution ------------------------------------------------------------


def test_each_executor_answers_to_its_stored_name():
    assert get_executor(SYSTEMD_EXECUTOR).name == SYSTEMD_EXECUTOR
    assert get_executor(DEFAULT_EXECUTOR).name == DEFAULT_EXECUTOR


def test_a_name_this_machine_does_not_have_is_a_result(monkeypatch):
    """A GPU node's executor read back on a box that has no such thing."""
    result = get_executor("gpu-node")

    assert isinstance(result, UnknownExecutor)
    assert "systemd-unit" in result.message


def test_no_name_means_what_this_machine_detected(monkeypatch):
    monkeypatch.setenv("WORKBENCH_EXECUTOR", DEFAULT_EXECUTOR)

    assert get_executor().name == DEFAULT_EXECUTOR


def test_both_executors_satisfy_the_protocol():
    """The claim the seam makes: a third one is a new class, not a refactor."""
    assert isinstance(SystemdUnitExecutor(), Executor)
    assert isinstance(LocalProcessExecutor(), Executor)


def test_available_executors_is_sorted():
    assert list(available_executors()) == sorted(available_executors())
