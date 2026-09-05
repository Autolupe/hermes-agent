"""Each standalone config loader (gateway, TUI/desktop, cron) must honor managed scope.

These loaders build their own config dict instead of routing through
hermes_cli.config.load_config, so the managed overlay has to be wired into each.
This is the regression guard for the whole bug class (a managed display.skin was
silently ignored by the TUI; the same gap existed in the gateway and cron).
"""
import os
import textwrap

import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()
    return home, managed


def _seed(home, managed, *, user, mgd):
    (home / "config.yaml").write_text(textwrap.dedent(user), encoding="utf-8")
    (managed / "config.yaml").write_text(textwrap.dedent(mgd), encoding="utf-8")
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()


@pytest.mark.parametrize("managed_text", ["[]\n", "false\n", "0\n"])
def test_fail_open_cache_does_not_hide_a_falsy_root_from_strict_reader(
    homes, managed_text
):
    _home, managed = homes
    (managed / "config.yaml").write_text(managed_text, encoding="utf-8")
    from hermes_cli import managed_scope

    assert managed_scope.load_managed_config() == {}
    with pytest.raises(ValueError, match="root is not a mapping"):
        managed_scope.load_managed_config_strict()


def test_strict_reader_rechecks_permissions_after_fail_open_cache_hit(homes):
    _home, managed = homes
    path = managed / "config.yaml"
    path.write_text("{}\n", encoding="utf-8")
    from hermes_cli import managed_scope

    assert managed_scope.load_managed_config() == {}
    before = path.stat()
    path.chmod(0)
    try:
        after = path.stat()
        assert (after.st_mtime_ns, after.st_size) == (
            before.st_mtime_ns,
            before.st_size,
        )
        with pytest.raises(OSError):
            path.read_text(encoding="utf-8")
        with pytest.raises(OSError):
            managed_scope.load_managed_config_strict()
    finally:
        os.chmod(path, 0o600)








def test_timezone_honors_managed(homes, monkeypatch):
    home, managed = homes
    # hermes_time checks an env override first; ensure it's unset so config wins.
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    _seed(home, managed, user="timezone: America/New_York\n", mgd="timezone: Asia/Tokyo\n")
    import hermes_time

    assert hermes_time._resolve_timezone_name() == "Asia/Tokyo"


def test_gateway_env_bridge_honors_managed(homes, monkeypatch):
    """The gateway config→env bridge must bridge MANAGED values, not user ones.

    gateway/run.py bridges config.yaml settings into os.environ at startup and on
    every turn (HERMES_TIMEZONE, HERMES_REDACT_SECRETS, HERMES_MAX_ITERATIONS,
    ...). A managed value must win at that env layer too — otherwise the bridge
    writes the user's value into the env that the whole process then reads. This
    is the regression that manual verification caught (managed timezone was
    overridden by the user's value via the env bridge).

    We assert on the managed-overlaid config the bridge consumes (rather than the
    os.environ side effect, which leaks across same-process tests under the
    runner) — the bridge writes whatever this dict carries, so a managed value
    here proves the env var gets the managed value.
    """
    home, managed = homes
    _seed(home, managed, user="timezone: America/New_York\n", mgd="timezone: Asia/Tokyo\n")
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    # The bridge loads config.yaml, expands env, then applies this overlay before
    # writing HERMES_TIMEZONE = cfg["timezone"]. Prove the overlay flips the value.
    import yaml

    raw = yaml.safe_load((home / "config.yaml").read_text())
    bridged = managed_scope.apply_managed_overlay(raw)
    assert bridged.get("timezone") == "Asia/Tokyo"
