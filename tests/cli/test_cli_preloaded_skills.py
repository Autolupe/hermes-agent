from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def _make_real_cli(**kwargs):
    clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), patch.dict(
        "os.environ", clean_env, clear=False
    ):
        import cli as cli_mod

        cli_mod = importlib.reload(cli_mod)
        with patch.object(cli_mod, "get_tool_definitions", return_value=[]), patch.dict(
            cli_mod.__dict__, {"CLI_CONFIG": clean_config}
        ):
            return cli_mod.HermesCLI(**kwargs)


class _DummyCLI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.session_id = "session-123"
        self.system_prompt = "base prompt"
        self.preloaded_skills = []

    def show_banner(self):
        return None

    def show_tools(self):
        return None

    def show_toolsets(self):
        return None

    def run(self):
        return None


def _real_finalize(cli_obj):
    """Call the real HermesCLI.finalize_preloaded_skills on a dummy object."""
    return _REAL_FINALIZE(cli_obj)


def _capture_real_finalize():
    import cli as cli_mod
    return cli_mod.HermesCLI.__dict__["finalize_preloaded_skills"]


_REAL_FINALIZE = _capture_real_finalize()


def test_main_applies_preloaded_skills_to_system_prompt(monkeypatch):
    import cli as cli_mod

    created = {}

    def fake_cli(**kwargs):
        created["cli"] = _DummyCLI(**kwargs)
        return created["cli"]

    monkeypatch.setattr(cli_mod, "HermesCLI", fake_cli)
    monkeypatch.setattr(
        cli_mod,
        "build_preloaded_skills_prompt",
        lambda skills, task_id=None: ("skill prompt", ["hermes-agent-dev", "github-auth"], []),
    )

    with pytest.raises(SystemExit):
        cli_mod.main(skills="hermes-agent-dev,github-auth", list_tools=True)

    cli_obj = created["cli"]
    # The preload now runs in a background thread and is folded in at agent
    # init via finalize_preloaded_skills() (startup-latency change). Drive
    # the finalize explicitly — the same call _init_agent makes.
    _real_finalize(cli_obj)
    assert cli_obj.system_prompt == "base prompt\n\nskill prompt"
    assert cli_obj.preloaded_skills == ["hermes-agent-dev", "github-auth"]


def test_main_raises_for_unknown_preloaded_skill(monkeypatch):
    import cli as cli_mod

    created = {}

    def fake_cli(**kwargs):
        created["cli"] = _DummyCLI(**kwargs)
        return created["cli"]

    monkeypatch.setattr(cli_mod, "HermesCLI", fake_cli)
    monkeypatch.setattr(
        cli_mod,
        "build_preloaded_skills_prompt",
        lambda skills, task_id=None: ("", [], ["missing-skill"]),
    )

    with pytest.raises(SystemExit):
        cli_mod.main(skills="missing-skill", list_tools=True)

    # The all-skills-unknown hard failure now surfaces when the preload is
    # finalized (agent init), preserving the fail-loud contract.
    with pytest.raises(ValueError, match=r"Unknown skill\(s\): missing-skill"):
        _real_finalize(created["cli"])


def test_show_banner_does_not_print_skills():
    """show_banner() no longer prints the activated skills line — it moved to run()."""
    cli_obj = _make_real_cli(compact=False)
    cli_obj.preloaded_skills = ["hermes-agent-dev", "github-auth"]
    cli_obj.console = MagicMock()

    with patch("cli.build_welcome_banner") as mock_banner, patch(
        "shutil.get_terminal_size", return_value=os.terminal_size((120, 40))
    ):
        cli_obj.show_banner()

    print_calls = [
        call.args[0]
        for call in cli_obj.console.print.call_args_list
        if call.args and isinstance(call.args[0], str)
    ]
    startup_lines = [line for line in print_calls if "Activated skills:" in line]
    assert len(startup_lines) == 0
    assert mock_banner.call_count == 1


class _RecordingPreloadThread:
    """Expose wait outcomes without sleeping or executing a skill loader."""

    def __init__(self, *, alive=False, join_error=None, alive_error=None):
        self.alive = alive
        self.join_error = join_error
        self.alive_error = alive_error
        self.calls = []

    def join(self, timeout=None):
        self.calls.append(("join", timeout))
        if self.join_error is not None:
            raise self.join_error

    def is_alive(self):
        self.calls.append(("is_alive",))
        if self.alive_error is not None:
            raise self.alive_error
        return self.alive


@pytest.fixture
def preload_cli():
    """Use real methods without CLI construction, credentials, or skill I/O."""
    import cli as cli_mod

    cli_obj = cli_mod.HermesCLI.__new__(cli_mod.HermesCLI)
    cli_obj.agent = None
    cli_obj.system_prompt = "base prompt"
    cli_obj.preloaded_skills = []
    cli_obj._preload_skills_thread = _RecordingPreloadThread()
    cli_obj._preload_skills_requested = ["requested-skill"]
    cli_obj._preload_skills_result = None
    cli_obj._preload_skills_error = None
    cli_obj._preload_skills_finalize_error = None
    cli_obj._preload_skills_finalized = False
    return cli_obj


def _assert_preload_not_applied(cli_obj):
    assert cli_obj.system_prompt == "base prompt"
    assert cli_obj.preloaded_skills == []
    assert cli_obj._preload_skills_finalized is False


@pytest.mark.parametrize("result_published", [False, True])
def test_finalize_refuses_a_still_running_thread(preload_cli, result_published):
    thread = preload_cli._preload_skills_thread
    thread.alive = True
    if result_published:
        preload_cli._preload_skills_result = ("too early", ["requested-skill"], [])

    with pytest.raises(TimeoutError, match="skills"):
        preload_cli.finalize_preloaded_skills()

    assert thread.calls == [("join", 120), ("is_alive",)]
    _assert_preload_not_applied(preload_cli)


@pytest.mark.parametrize("late_worker_error", [False, True])
def test_finalize_timeout_stays_failed_after_late_completion(
    preload_cli, late_worker_error
):
    thread = preload_cli._preload_skills_thread
    thread.alive = True
    with pytest.raises(TimeoutError) as first:
        preload_cli.finalize_preloaded_skills()

    thread.alive = False
    preload_cli._preload_skills_result = ("late prompt", ["requested-skill"], [])
    preload_cli._preload_skills_error = (
        OSError("late worker failure") if late_worker_error else None
    )
    for _ in range(2):
        with pytest.raises(TimeoutError) as repeated:
            preload_cli.finalize_preloaded_skills()
        assert repeated.value is first.value
        _assert_preload_not_applied(preload_cli)

    assert thread.calls == [("join", 120), ("is_alive",)]


@pytest.mark.parametrize("failure_stage", ["join", "is_alive"])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt, SystemExit])
def test_finalize_wait_failure_is_sticky(preload_cli, failure_stage, error_type):
    thread = preload_cli._preload_skills_thread
    failure = error_type("wait failed")
    if failure_stage == "join":
        thread.join_error = failure
        expected_calls = [("join", 120)]
    else:
        thread.alive_error = failure
        expected_calls = [("join", 120), ("is_alive",)]

    with pytest.raises(error_type) as first:
        preload_cli.finalize_preloaded_skills()
    assert first.value is failure
    _assert_preload_not_applied(preload_cli)

    # Simulate a later healthy thread and a separately changed producer slot.
    thread.join_error = None
    thread.alive_error = None
    preload_cli._preload_skills_result = ("late prompt", ["requested-skill"], [])
    preload_cli._preload_skills_error = RuntimeError("later producer failure")
    for _ in range(2):
        with pytest.raises(error_type) as repeated:
            preload_cli.finalize_preloaded_skills()
        assert repeated.value is failure
        _assert_preload_not_applied(preload_cli)

    assert thread.calls == expected_calls


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, KeyboardInterrupt, SystemExit])
def test_finalize_worker_failure_is_repeatably_raised(preload_cli, error_type):
    failure = error_type("skill loader failed")
    preload_cli._preload_skills_error = failure
    # A producer error takes precedence even when a result is also present.
    preload_cli._preload_skills_result = ("unused prompt", ["requested-skill"], [])

    with pytest.raises(error_type) as first:
        preload_cli.finalize_preloaded_skills()
    assert first.value is failure
    _assert_preload_not_applied(preload_cli)

    preload_cli._preload_skills_error = None
    for _ in range(2):
        with pytest.raises(error_type) as repeated:
            preload_cli.finalize_preloaded_skills()
        assert repeated.value is failure
        _assert_preload_not_applied(preload_cli)

    assert preload_cli._preload_skills_thread.calls == [("join", 120), ("is_alive",)]


def test_finalize_all_missing_is_repeatably_raised(preload_cli):
    preload_cli._preload_skills_result = ("", [], ["missing-one", "missing-two"])

    with pytest.raises(ValueError, match="Unknown skill") as first:
        preload_cli.finalize_preloaded_skills()
    assert "missing-one" in str(first.value)
    assert "missing-two" in str(first.value)
    _assert_preload_not_applied(preload_cli)

    preload_cli._preload_skills_result = ("late prompt", ["requested-skill"], [])
    for _ in range(2):
        with pytest.raises(ValueError) as repeated:
            preload_cli.finalize_preloaded_skills()
        assert repeated.value is first.value
        _assert_preload_not_applied(preload_cli)

    assert preload_cli._preload_skills_thread.calls == [("join", 120), ("is_alive",)]


@pytest.mark.parametrize("result_slot_exists", [False, True])
def test_finalize_completed_thread_without_result_stays_failed(
    preload_cli, result_slot_exists
):
    if not result_slot_exists:
        del preload_cli._preload_skills_result

    with pytest.raises(RuntimeError, match="without a result") as first:
        preload_cli.finalize_preloaded_skills()
    _assert_preload_not_applied(preload_cli)

    preload_cli._preload_skills_result = ("late prompt", ["requested-skill"], [])
    for _ in range(2):
        with pytest.raises(RuntimeError) as repeated:
            preload_cli.finalize_preloaded_skills()
        assert repeated.value is first.value
        _assert_preload_not_applied(preload_cli)

    assert preload_cli._preload_skills_thread.calls == [("join", 120), ("is_alive",)]


@pytest.mark.parametrize("base_prompt", ["base prompt", ""])
@pytest.mark.parametrize("missing_skills", [[], ["missing-skill"]])
def test_finalize_success_applies_prompt_once(preload_cli, caplog, base_prompt, missing_skills):
    preload_cli.system_prompt = base_prompt
    preload_cli._preload_skills_result = (
        "skill prompt",
        ["requested-skill"],
        missing_skills,
    )

    for _ in range(3):
        preload_cli.finalize_preloaded_skills()

    expected_prompt = f"{base_prompt}\n\nskill prompt" if base_prompt else "skill prompt"
    assert preload_cli.system_prompt == expected_prompt
    assert preload_cli.preloaded_skills == ["requested-skill"]
    assert preload_cli._preload_skills_finalized is True
    assert preload_cli._preload_skills_thread.calls == [("join", 120), ("is_alive",)]
    warnings = [record for record in caplog.records if "Unknown skill(s)" in record.message]
    if missing_skills:
        assert len(warnings) == 1
        assert "missing-skill" in warnings[0].message
        assert "requested-skill" in warnings[0].message
    else:
        assert warnings == []


def test_finalize_reads_result_after_join_completes(preload_cli, monkeypatch):
    thread = preload_cli._preload_skills_thread
    thread.alive = True

    def complete_on_join(timeout=None):
        thread.calls.append(("join", timeout))
        preload_cli._preload_skills_result = ("skill prompt", ["requested-skill"], [])
        thread.alive = False

    monkeypatch.setattr(thread, "join", complete_on_join)
    for _ in range(2):
        preload_cli.finalize_preloaded_skills()

    assert preload_cli.system_prompt == "base prompt\n\nskill prompt"
    assert preload_cli.preloaded_skills == ["requested-skill"]
    assert preload_cli._preload_skills_finalized is True
    assert thread.calls == [("join", 120), ("is_alive",)]


def test_finalize_empty_requested_result_is_idempotent(preload_cli):
    preload_cli._preload_skills_requested = []
    preload_cli._preload_skills_result = ("", [], [])

    for _ in range(3):
        preload_cli.finalize_preloaded_skills()

    assert preload_cli.system_prompt == "base prompt"
    assert preload_cli.preloaded_skills == []
    assert preload_cli._preload_skills_finalized is True
    assert preload_cli._preload_skills_thread.calls == [("join", 120), ("is_alive",)]


@pytest.mark.parametrize("thread_slot_exists", [False, True])
def test_finalize_without_preload_thread_is_idempotent(preload_cli, thread_slot_exists):
    preload_cli._preload_skills_requested = []
    if thread_slot_exists:
        preload_cli._preload_skills_thread = None
    else:
        del preload_cli._preload_skills_thread

    for _ in range(3):
        preload_cli.finalize_preloaded_skills()

    assert preload_cli.system_prompt == "base prompt"
    assert preload_cli.preloaded_skills == []
    assert preload_cli._preload_skills_finalized is True


@pytest.fixture
def startup_traps(monkeypatch, preload_cli):
    import cli as cli_mod
    import hermes_cli.mcp_startup as mcp_startup

    targets = [
        (cli_mod, "_prepare_deferred_agent_startup"),
        (cli_mod, "AIAgent"),
        (preload_cli, "_install_tool_callbacks"),
        (preload_cli, "_ensure_tirith_security"),
        (preload_cli, "_ensure_runtime_credentials"),
        (mcp_startup, "ensure_mcp_discovery_before_agent_build"),
    ]
    traps = {}
    for owner, name in targets:
        trap = MagicMock(side_effect=AssertionError(f"Unexpected startup call: {name}"))
        monkeypatch.setattr(owner, name, trap)
        traps[name] = trap
    return traps


@pytest.mark.parametrize(
    ("failure_stage", "error_type"),
    [
        ("timeout", TimeoutError),
        ("join", OSError),
        ("is_alive", KeyboardInterrupt),
        ("worker", SystemExit),
        ("all_missing", ValueError),
        ("missing_result", RuntimeError),
    ],
)
def test_init_agent_refuses_failed_preload_before_any_startup(
    preload_cli, startup_traps, failure_stage, error_type
):
    thread = preload_cli._preload_skills_thread
    if failure_stage == "timeout":
        thread.alive = True
    elif failure_stage == "join":
        thread.join_error = error_type("wait failed")
    elif failure_stage == "is_alive":
        thread.alive_error = error_type("wait failed")
    elif failure_stage == "worker":
        preload_cli._preload_skills_error = error_type("loader failed")
    elif failure_stage == "all_missing":
        preload_cli._preload_skills_result = ("", [], ["missing-skill"])

    with pytest.raises(error_type) as first:
        preload_cli._init_agent()
    _assert_preload_not_applied(preload_cli)

    thread.alive = False
    thread.join_error = None
    thread.alive_error = None
    preload_cli._preload_skills_error = None
    preload_cli._preload_skills_result = ("late prompt", ["requested-skill"], [])
    for _ in range(2):
        with pytest.raises(error_type) as repeated:
            preload_cli._init_agent()
        assert repeated.value is first.value
        _assert_preload_not_applied(preload_cli)
        assert preload_cli.agent is None
        for trap in startup_traps.values():
            trap.assert_not_called()

    expected_calls = [("join", 120)]
    if failure_stage != "join":
        expected_calls.append(("is_alive",))
    assert thread.calls == expected_calls


def test_init_agent_success_finalizes_before_deferred_startup(preload_cli, startup_traps):
    preload_cli._preload_skills_result = ("skill prompt", ["requested-skill"], [])

    class StartupReached(Exception):
        pass

    def stop_after_preload():
        assert preload_cli.system_prompt == "base prompt\n\nskill prompt"
        assert preload_cli.preloaded_skills == ["requested-skill"]
        assert preload_cli._preload_skills_finalized is True
        raise StartupReached

    startup_traps["_prepare_deferred_agent_startup"].side_effect = stop_after_preload
    with pytest.raises(StartupReached):
        preload_cli._init_agent()

    startup_traps["_prepare_deferred_agent_startup"].assert_called_once_with()
    for name, trap in startup_traps.items():
        if name != "_prepare_deferred_agent_startup":
            trap.assert_not_called()
    assert preload_cli.agent is None
    assert preload_cli._preload_skills_thread.calls == [("join", 120), ("is_alive",)]
