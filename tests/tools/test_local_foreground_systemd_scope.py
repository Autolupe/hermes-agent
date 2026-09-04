"""Foreground local commands stay outside supervised web-service cgroups."""

from __future__ import annotations

import builtins
import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools import process_registry as process_registry_mod
from tools.environments import base as base_mod
from tools.environments import local as local_mod
from tools.environments.local import LocalEnvironment


class _FinishedProc:
    """Small Popen-compatible handle for output/exit contract coverage."""

    pid = 987_654_321
    returncode = 7
    stdin = None

    def __init__(self):
        self.stdout = iter(["scoped output\n"])

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -signal.SIGKILL


class _RunningProc:
    """Never-exiting handle used to drive timeout and interrupt cleanup."""

    pid = 987_654_322
    returncode = None
    stdin = None

    def __init__(self, unit: str):
        self.stdout = iter(())
        self._hermes_systemd_unit = unit
        self.wait_calls = []
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        self.returncode = 0
        return 0

    def kill(self):
        self.kill_calls += 1
        self.returncode = -signal.SIGKILL


class _LateRegistrationProc(_RunningProc):
    """Wrapper whose scope appears only after immediate cancellation."""

    def __init__(self, unit: str):
        super().__init__(unit)
        self.registration_pending = False
        self.child_alive = True

    def kill(self):
        self.registration_pending = True
        super().kill()


@pytest.fixture()
def bare_local_env(tmp_path):
    """LocalEnvironment without its constructor's shell snapshot spawn."""
    env = object.__new__(LocalEnvironment)
    env.cwd = str(tmp_path)
    env.env = {}
    return env


def _set_cgroup(
    monkeypatch, unit: str = "", *, user_manager: bool | None = True
) -> None:
    if not unit:
        cgroup = "0::/user.slice/user-1000.slice/session-42.scope\n"
    elif user_manager:
        cgroup = (
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
            f"{unit}\n"
        )
    elif user_manager is False:
        cgroup = f"0::/system.slice/{unit}\n"
    else:
        cgroup = f"0::/custom.slice/{unit}\n"

    def fake_read_text(path: Path, **_kwargs):
        if str(path) == "/proc/self/cgroup":
            return cgroup
        raise OSError("test does not expose other cgroup files")

    monkeypatch.setattr(process_registry_mod.Path, "read_text", fake_read_text)


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "unit",
    ["hermes-serve.service", "hermes-dashboard.service"],
)
def test_web_service_cgroups_are_supervised_runtimes(monkeypatch, unit):
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    _set_cgroup(monkeypatch, unit)

    assert process_registry_mod._is_supervised_gateway_process() is True


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "unit",
    ["hermes-serve.service", "hermes-dashboard.service"],
)
def test_system_service_cgroups_are_not_user_scope_supervisors(monkeypatch, unit):
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    _set_cgroup(monkeypatch, unit, user_manager=False)

    assert process_registry_mod._is_supervised_gateway_process() is False


@pytest.mark.linux_only
@pytest.mark.parametrize(
    ("user_manager", "expected"),
    [
        (True, ("hermes-serve.service", True)),
        (False, ("hermes-serve.service", False)),
    ],
)
def test_web_runtime_owner_records_the_correct_systemd_manager(
    monkeypatch, user_manager, expected
):
    _set_cgroup(
        monkeypatch,
        "hermes-serve.service",
        user_manager=user_manager,
    )

    assert process_registry_mod._supervised_runtime_owner() == expected


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "unit",
    [
        "hermes-gateway.service",
        "hermes-gateway-coder.service",
        "hermes-gateway-a1b2c3d4.service",
    ],
)
def test_profile_scoped_gateway_owner_is_recognized(monkeypatch, unit):
    _set_cgroup(monkeypatch, unit)

    assert local_mod._may_need_foreground_systemd_scope() is True
    assert process_registry_mod._supervised_runtime_owner() == (unit, True)


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "unit",
    [
        "hermes-serve-work.service",
        "hermes-serve-a1b2c3d4.service",
        "hermes-serve-team_one.service",
    ],
)
def test_profile_scoped_serve_owner_is_recognized(monkeypatch, unit):
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    _set_cgroup(monkeypatch, unit)

    assert local_mod._may_need_foreground_systemd_scope() is True
    assert process_registry_mod._supervised_runtime_owner() == (unit, True)
    assert process_registry_mod._is_supervised_gateway_process() is True


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "unit",
    [
        "hermes-server.service",
        "hermes-serve-.service",
        "hermes-serve-work.timer",
        "other-hermes-serve-work.service",
    ],
)
def test_serve_lookalike_units_are_not_supervised(monkeypatch, unit):
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    _set_cgroup(monkeypatch, unit)

    assert local_mod._may_need_foreground_systemd_scope() is False
    assert process_registry_mod._supervised_runtime_owner() == ("", False)
    assert process_registry_mod._is_supervised_gateway_process() is False


@pytest.mark.linux_only
def test_profile_scoped_gateway_foreground_scope_binds_to_exact_owner(monkeypatch):
    unit = "hermes-gateway-coder.service"
    _set_cgroup(monkeypatch, unit)
    monkeypatch.setattr(
        process_registry_mod, "_is_supervised_gateway_process", lambda: True
    )
    monkeypatch.setattr(
        process_registry_mod, "_systemd_run_user_scope_available", lambda: True
    )
    monkeypatch.setattr(
        process_registry_mod,
        "_systemd_scope_stop_propagation_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None,
    )

    direct = ["/bin/bash", "-c", "printf scoped"]
    argv, scope_unit = local_mod._foreground_systemd_scope_argv(direct)

    assert scope_unit.startswith(f"hermes-worker-foreground-{os.getpid()}-")
    assert f"BindsTo={unit}" in argv
    assert f"After={unit}" in argv
    assert f"StopPropagatedFrom={unit}" in argv


@pytest.mark.linux_only
def test_foreground_spawn_binds_scope_to_owner_and_preserves_result_contract(
    bare_local_env,
    monkeypatch,
):
    captured = {}
    proc = _FinishedProc()

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(local_mod, "_find_bash", lambda: "/bin/bash")
    monkeypatch.setattr(local_mod, "_make_run_env", lambda _env: {"PATH": "/usr/bin"})
    monkeypatch.setattr(local_mod.subprocess, "Popen", fake_popen)
    _set_cgroup(monkeypatch, "hermes-serve.service")
    monkeypatch.setattr(
        process_registry_mod, "_systemd_run_user_scope_available", lambda: True
    )
    monkeypatch.setattr(
        process_registry_mod,
        "_systemd_scope_stop_propagation_available",
        lambda: True,
    )
    monkeypatch.setattr(
        process_registry_mod,
        "_SYSTEMD_RUN_EXPAND_ENVIRONMENT_SUPPORTED",
        True,
    )
    monkeypatch.setattr(
        process_registry_mod, "_worker_memory_max_bytes", lambda: 512 * 1024 * 1024
    )
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None,
    )

    returned = bare_local_env._run_bash("printf scoped-output")

    assert returned is proc
    argv = captured["argv"]
    assert argv[0] == "/usr/bin/systemd-run"
    assert "--user" in argv
    assert "--scope" in argv
    assert "--collect" in argv
    assert "--" in argv
    assert argv[-3:] == ["/bin/bash", "-c", "printf scoped-output"]
    unit_index = argv.index("--unit")
    unit = argv[unit_index + 1]
    assert unit.startswith(f"hermes-worker-foreground-{os.getpid()}-")
    assert proc._hermes_systemd_unit == f"{unit}.scope"
    assert "BindsTo=hermes-serve.service" in argv
    assert "After=hermes-serve.service" in argv
    assert "StopPropagatedFrom=hermes-serve.service" in argv
    assert "TimeoutStopSec=3s" in argv
    assert "--expand-environment=no" in argv
    assert not any(value.startswith("PartOf=") for value in argv)
    assert not any(value.startswith("RuntimeMaxSec=") for value in argv)

    kwargs = captured["kwargs"]
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["start_new_session"] is True

    result = bare_local_env._wait_for_process(returned, timeout=1)
    assert result == {"output": "scoped output\n", "returncode": 7}


@pytest.mark.parametrize("gateway_marker", [None, "1"])
def test_foreground_spawn_keeps_direct_path_without_registry_import(
    bare_local_env,
    monkeypatch,
    gateway_marker,
):
    captured = {}
    proc = _FinishedProc()

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(local_mod, "_find_bash", lambda: "/bin/bash")
    monkeypatch.setattr(local_mod, "_make_run_env", lambda _env: {})
    monkeypatch.setattr(local_mod, "windows_hide_flags", lambda: 0)
    monkeypatch.setattr(local_mod.subprocess, "Popen", fake_popen)
    if gateway_marker is None:
        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    else:
        monkeypatch.setenv("_HERMES_GATEWAY", gateway_marker)
    _set_cgroup(monkeypatch)

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "tools.process_registry":
            pytest.fail("ordinary local calls must not import process_registry")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    returned = bare_local_env._run_bash("printf direct-output")

    assert returned is proc
    assert captured["argv"] == ["/bin/bash", "-c", "printf direct-output"]
    assert not hasattr(proc, "_hermes_systemd_unit")
    assert captured["kwargs"]["start_new_session"] is True


@pytest.mark.linux_only
@pytest.mark.parametrize("user_manager", [False, None])
def test_non_user_service_owner_keeps_direct_spawn_without_lifetime_cap(
    monkeypatch, user_manager
):
    _set_cgroup(
        monkeypatch,
        "hermes-dashboard.service",
        user_manager=user_manager,
    )
    monkeypatch.setattr(
        process_registry_mod,
        "_systemd_run_user_scope_available",
        lambda: pytest.fail("non-user owners must not probe a user scope"),
    )

    direct = ["/bin/bash", "-c", "setsid sleep 300 & disown"]
    argv, unit = local_mod._foreground_systemd_scope_argv(direct)

    assert argv == direct
    assert unit == ""
    assert not any(value.startswith("RuntimeMaxSec=") for value in argv)


@pytest.mark.linux_only
def test_old_systemd_without_stop_propagation_keeps_foreground_spawn_direct(
    monkeypatch,
):
    _set_cgroup(monkeypatch, "hermes-serve.service")
    monkeypatch.setattr(
        process_registry_mod, "_systemd_run_user_scope_available", lambda: True
    )
    monkeypatch.setattr(
        process_registry_mod,
        "_systemd_scope_stop_propagation_available",
        lambda: False,
    )

    direct = ["/bin/bash", "-c", "printf direct"]
    argv, unit = local_mod._foreground_systemd_scope_argv(direct)

    assert argv == direct
    assert unit == ""


@pytest.mark.linux_only
def test_scope_probe_enables_literal_argv_and_stop_propagation(monkeypatch):
    calls = []

    monkeypatch.setattr(process_registry_mod, "_SYSTEMD_SCOPE_AVAILABLE", None)
    monkeypatch.setattr(
        process_registry_mod,
        "_SYSTEMD_RUN_EXPAND_ENVIRONMENT_SUPPORTED",
        None,
    )
    monkeypatch.setattr(
        process_registry_mod,
        "_SYSTEMD_SCOPE_STOP_PROPAGATION_SUPPORTED",
        None,
    )
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None,
    )

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode=0, stderr=b"")

    monkeypatch.setattr(process_registry_mod.subprocess, "run", fake_run)

    assert process_registry_mod._systemd_run_user_scope_available() is True
    script = "printf '%s\\n' '$HERMES_SCOPE_LITERAL_UNSET'"
    argv = process_registry_mod._build_systemd_scope_argv(
        ["/bin/bash", "-c", script],
        unit_suffix="literal-test",
        binds_to_unit="hermes-serve.service",
        stop_propagated_from_unit="hermes-serve.service",
    )

    assert len(calls) == 1
    assert "--expand-environment=no" in calls[0]
    assert "StopPropagatedFrom=basic.target" in calls[0]
    assert "--expand-environment=no" in argv
    assert "StopPropagatedFrom=hermes-serve.service" in argv
    assert argv[-3:] == ["/bin/bash", "-c", script]


@pytest.mark.linux_only
def test_scope_probe_falls_back_on_pre_v249_systemd(monkeypatch):
    calls = []

    monkeypatch.setattr(process_registry_mod, "_SYSTEMD_SCOPE_AVAILABLE", None)
    monkeypatch.setattr(
        process_registry_mod,
        "_SYSTEMD_RUN_EXPAND_ENVIRONMENT_SUPPORTED",
        None,
    )
    monkeypatch.setattr(
        process_registry_mod,
        "_SYSTEMD_SCOPE_STOP_PROPAGATION_SUPPORTED",
        None,
    )
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None,
    )

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        if "--expand-environment=no" in argv:
            return subprocess.CompletedProcess(
                argv,
                returncode=1,
                stderr=b"systemd-run: unrecognized option '--expand-environment=no'",
            )
        if "StopPropagatedFrom=basic.target" in argv:
            return subprocess.CompletedProcess(
                argv,
                returncode=1,
                stderr=b"Unknown assignment: StopPropagatedFrom=basic.target",
            )
        return subprocess.CompletedProcess(argv, returncode=0, stderr=b"")

    monkeypatch.setattr(process_registry_mod.subprocess, "run", fake_run)

    assert process_registry_mod._systemd_run_user_scope_available() is True
    assert len(calls) == 3
    assert process_registry_mod._SYSTEMD_RUN_EXPAND_ENVIRONMENT_SUPPORTED is False
    assert process_registry_mod._SYSTEMD_SCOPE_STOP_PROPAGATION_SUPPORTED is False
    argv = process_registry_mod._build_systemd_scope_argv(
        ["/bin/bash", "-c", "printf '$UNSET'"],
        unit_suffix="old-systemd",
    )
    assert "--expand-environment=no" not in argv
    assert not any(value.startswith("StopPropagatedFrom=") for value in argv)


@pytest.mark.linux_only
@pytest.mark.parametrize(
    ("interrupted", "timeout", "expected_code", "expected_text"),
    [
        (True, 60, 130, "interrupted"),
        (False, -1, 124, "timed out"),
    ],
)
def test_timeout_and_interrupt_stop_the_whole_foreground_scope(
    bare_local_env,
    monkeypatch,
    interrupted,
    timeout,
    expected_code,
    expected_text,
):
    unit = "hermes-worker-foreground-test.scope"
    proc = _RunningProc(unit)
    stopped = []
    statuses = iter(
        [
            process_registry_mod._SYSTEMD_UNIT_STOPPED,
            process_registry_mod._SYSTEMD_UNIT_ABSENT,
        ]
    )

    monkeypatch.setattr(base_mod, "is_interrupted", lambda: interrupted)
    monkeypatch.setattr(
        process_registry_mod,
        "_stop_systemd_unit_status",
        lambda name, *, aggressive: (
            stopped.append((name, aggressive)) or next(statuses)
        ),
    )
    monkeypatch.setattr(
        local_mod.os,
        "getpgid",
        lambda _pid: pytest.fail("scoped cleanup must not stop at wrapper pgid"),
    )

    result = bare_local_env._wait_for_process(proc, timeout=timeout)

    assert result["returncode"] == expected_code
    assert expected_text in result["output"].lower()
    assert stopped == [(unit, True), (unit, True)]
    assert proc.wait_calls == [local_mod._FOREGROUND_SCOPE_WRAPPER_REAP_SECONDS]
    assert proc.kill_calls == 0


@pytest.mark.linux_only
def test_immediate_interrupt_stops_a_scope_that_registers_after_wrapper_reap(
    bare_local_env,
    monkeypatch,
):
    unit = "hermes-worker-foreground-late.scope"
    proc = _LateRegistrationProc(unit)
    stop_calls = []
    statuses = iter(
        [
            process_registry_mod._SYSTEMD_UNIT_ABSENT,
            process_registry_mod._SYSTEMD_UNIT_ABSENT,
            process_registry_mod._SYSTEMD_UNIT_STOPPED,
        ]
    )

    def stop_status(name, *, aggressive):
        stop_calls.append((name, aggressive))
        status = next(statuses)
        if len(stop_calls) >= 2:
            assert proc.registration_pending is True
        if status == process_registry_mod._SYSTEMD_UNIT_STOPPED:
            proc.child_alive = False
        return status

    monkeypatch.setattr(base_mod, "is_interrupted", lambda: True)
    monkeypatch.setattr(
        local_mod,
        "_FOREGROUND_SCOPE_REGISTRATION_RETRY_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        local_mod,
        "_FOREGROUND_SCOPE_POST_REAP_RECHECK_SECONDS",
        0.1,
    )
    monkeypatch.setattr(
        process_registry_mod,
        "_stop_systemd_unit_status",
        stop_status,
    )
    monkeypatch.setattr(
        local_mod.os,
        "getpgid",
        lambda _pid: pytest.fail("late registered scope must not use pgid fallback"),
    )

    result = bare_local_env._wait_for_process(proc, timeout=60)

    assert result["returncode"] == 130
    assert "interrupted" in result["output"].lower()
    assert stop_calls == [(unit, True), (unit, True), (unit, True)]
    assert proc.kill_calls == 1
    assert proc.registration_pending is True
    assert proc.child_alive is False


@pytest.mark.linux_only
def test_failed_scope_stop_falls_back_to_existing_process_group_cleanup(
    bare_local_env,
    monkeypatch,
):
    unit = "hermes-worker-foreground-test.scope"
    proc = _RunningProc(unit)
    proc._hermes_pgid = 43_210
    killpg_calls = []

    monkeypatch.setattr(
        process_registry_mod,
        "_stop_systemd_unit_status",
        lambda _name, *, aggressive: (
            process_registry_mod._SYSTEMD_UNIT_FAILED
            if aggressive
            else pytest.fail("foreground cleanup must request aggressive mode")
        ),
    )
    monkeypatch.setattr(local_mod.os, "getpgid", lambda _pid: proc._hermes_pgid)

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(local_mod.os, "killpg", fake_killpg)

    bare_local_env._kill_process(proc)

    assert killpg_calls == [
        (proc._hermes_pgid, signal.SIGTERM),
        (proc._hermes_pgid, 0),
    ]


@pytest.mark.linux_only
def test_scope_stop_timeout_force_kills_every_remaining_cgroup_member(monkeypatch):
    unit = "hermes-worker-foreground-ignores-term.scope"
    calls = []

    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
    )

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs["timeout"]))
        if "stop" in argv:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, returncode=0, stderr=b"")

    monkeypatch.setattr(process_registry_mod.subprocess, "run", fake_run)

    assert (
        process_registry_mod._stop_systemd_unit_status(unit, aggressive=True)
        == process_registry_mod._SYSTEMD_UNIT_STOPPED
    )
    assert calls == [
        (
            ["/usr/bin/systemctl", "--user", "stop", unit],
            process_registry_mod._FOREGROUND_SCOPE_STOP_TIMEOUT_SECONDS,
        ),
        (
            [
                "/usr/bin/systemctl",
                "--user",
                "kill",
                "--kill-whom=all",
                "--signal=SIGKILL",
                unit,
            ],
            process_registry_mod._FOREGROUND_SCOPE_KILL_TIMEOUT_SECONDS,
        ),
    ]


@pytest.mark.linux_only
def test_background_scope_stop_keeps_original_grace_without_forced_kill(monkeypatch):
    unit = "hermes-worker-background-grace.scope"
    calls = []

    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None,
    )

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs["timeout"]))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(process_registry_mod.subprocess, "run", fake_run)

    assert process_registry_mod._stop_systemd_unit(unit) is False
    assert calls == [
        (
            ["/usr/bin/systemctl", "--user", "stop", unit],
            process_registry_mod._SYSTEMD_UNIT_STOP_TIMEOUT_SECONDS,
        )
    ]


@pytest.mark.linux_only
def test_serve_cgroup_keeps_background_pty_on_the_existing_scope_path(
    tmp_path,
    monkeypatch,
):
    from ptyprocess import PtyProcess

    _set_cgroup(monkeypatch, "hermes-serve.service")
    monkeypatch.setattr(
        process_registry_mod, "_systemd_run_user_scope_available", lambda: True
    )
    monkeypatch.setattr(
        process_registry_mod, "_worker_memory_max_bytes", lambda: 512 * 1024 * 1024
    )
    monkeypatch.setattr(process_registry_mod, "_find_shell", lambda: "/bin/bash")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None,
    )

    fake_pty = MagicMock(pid=43_211)
    registry = process_registry_mod.ProcessRegistry()
    with (
        patch.object(PtyProcess, "spawn", return_value=fake_pty) as pty_spawn,
        patch("tools.process_registry.threading.Thread", return_value=MagicMock()),
        patch.object(registry, "_write_checkpoint"),
    ):
        session = registry.spawn_local("codex", cwd=str(tmp_path), use_pty=True)

    argv = pty_spawn.call_args.args[0]
    assert argv[0] == "/usr/bin/systemd-run"
    assert "--scope" in argv
    assert argv[-3:] == ["/bin/bash", "-lic", "set +m; codex"]
    assert session.systemd_unit == f"hermes-worker-{session.id}.scope"
    assert not any(value.startswith("TimeoutStopSec=") for value in argv)
    assert not any(value.startswith("StopPropagatedFrom=") for value in argv)


@pytest.mark.linux_only
@pytest.mark.parametrize("user_manager", [False, None])
def test_non_user_serve_cgroup_keeps_background_pty_on_direct_path(
    tmp_path,
    monkeypatch,
    user_manager,
):
    from ptyprocess import PtyProcess

    _set_cgroup(
        monkeypatch,
        "hermes-serve.service",
        user_manager=user_manager,
    )
    monkeypatch.setattr(process_registry_mod, "_find_shell", lambda: "/bin/bash")
    monkeypatch.setattr(
        process_registry_mod,
        "_systemd_run_user_scope_available",
        lambda: pytest.fail("non-user background calls must not probe user systemd"),
    )

    fake_pty = MagicMock(pid=43_212)
    registry = process_registry_mod.ProcessRegistry()
    with (
        patch.object(PtyProcess, "spawn", return_value=fake_pty) as pty_spawn,
        patch("tools.process_registry.threading.Thread", return_value=MagicMock()),
        patch.object(registry, "_write_checkpoint"),
    ):
        session = registry.spawn_local("codex", cwd=str(tmp_path), use_pty=True)

    assert pty_spawn.call_args.args[0] == [
        "/bin/bash",
        "-lic",
        "set +m; codex",
    ]
    assert session.systemd_unit == ""
