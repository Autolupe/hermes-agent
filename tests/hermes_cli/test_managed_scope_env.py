"""Env integration tests — managed .env applied last with override."""
import os
from pathlib import Path

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


def test_unreadable_managed_env_does_not_block_user_env(env_homes, monkeypatch):
    """A managed child lookup failure must not crash gateway-style loading."""
    from hermes_cli.env_loader import load_hermes_dotenv

    home, managed = env_homes
    user_env = home / ".env"
    managed_env = managed / ".env"
    user_env.write_text("MANAGED_LOOKUP_TEST=from_user\n", encoding="utf-8")
    original_exists = Path.exists

    def permission_denied_for_managed_env(path):
        if path == managed_env:
            raise PermissionError(13, "Permission denied", str(path))
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", permission_denied_for_managed_env)

    load_hermes_dotenv(hermes_home=str(home))

    assert os.environ["MANAGED_LOOKUP_TEST"] == "from_user"
