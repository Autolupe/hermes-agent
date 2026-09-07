"""Env integration tests — managed .env applied last with override."""
import os

import pytest


@pytest.fixture
def env_homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    return home, managed


def test_managed_env_beats_user_env(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    (home / ".env").write_text("OPENAI_API_BASE=https://user.example/v1\n", encoding="utf-8")
    (managed / ".env").write_text("OPENAI_API_BASE=https://org.example/v1\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["OPENAI_API_BASE"] == "https://org.example/v1"


def test_no_managed_env_is_noop(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes  # managed dir exists but has no .env
    monkeypatch.setenv("SOME_VALUE", "from_shell")
    (home / ".env").write_text("SOME_VALUE=from_user\n", encoding="utf-8")
    load_hermes_dotenv(hermes_home=str(home))
    assert os.environ["SOME_VALUE"] == "from_user"


@pytest.mark.linux_only
def test_unreadable_managed_directory_does_not_block_user_env(env_homes, monkeypatch):
    from hermes_cli.env_loader import load_hermes_dotenv

    if os.geteuid() == 0:
        pytest.skip("The root account bypasses directory access permissions")
    home, managed = env_homes
    monkeypatch.delenv("MANAGED_PERMISSION_TEST", raising=False)
    user_env = home / ".env"
    user_env.write_text("MANAGED_PERMISSION_TEST=from_user\n", encoding="utf-8")
    managed_env = managed / ".env"
    managed_env.write_text("MANAGED_PERMISSION_TEST=from_managed\n", encoding="utf-8")
    managed.chmod(0)
    try:
        with pytest.raises(PermissionError):
            managed_env.stat()
        assert load_hermes_dotenv(hermes_home=str(home)) == [user_env]
        assert os.environ["MANAGED_PERMISSION_TEST"] == "from_user"
    finally:
        managed.chmod(0o700)
