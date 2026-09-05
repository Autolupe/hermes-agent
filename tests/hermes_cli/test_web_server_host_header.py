"""Tests for GHSA-ppp5-vxwm-4cf7 — Host-header validation.

DNS rebinding defence: a victim browser that has the dashboard open
could be tricked into fetching from an attacker-controlled hostname
that TTL-flips to 127.0.0.1. Same-origin / CORS checks won't help —
the browser now treats the attacker origin as same-origin. Validating
the Host header at the application layer rejects the attack.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_repo = str(Path(__file__).resolve().parents[1])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


class TestHostHeaderValidator:
    """Unit test the _is_accepted_host helper directly — cheaper and
    more thorough than spinning up the full FastAPI app."""



    def test_zero_zero_bind_accepts_anything_without_allowlist(
        self, tmp_path, monkeypatch
    ):
        """Preserve the legacy wildcard behavior without an explicit list."""
        from hermes_cli.web_server import _is_accepted_host

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "dashboard:\n  allowed_hosts: []\n", encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)
        for host in ("10.0.0.5", "evil.example", "my-server.corp.net"):
            assert _is_accepted_host(host, "0.0.0.0")
            assert _is_accepted_host(host + ":9119", "0.0.0.0")

    def test_wildcard_bind_uses_configured_host_allowlist(self, monkeypatch):
        from hermes_cli.web_server import _is_accepted_host

        monkeypatch.setenv(
            "HERMES_DASHBOARD_ALLOWED_HOSTS",
            "127.0.0.1,100.115.1.128,openclaw-cax41.tail465e59.ts.net",
        )

        assert _is_accepted_host("127.0.0.1:9120", "0.0.0.0")
        assert _is_accepted_host("100.115.1.128:9120", "0.0.0.0")
        assert _is_accepted_host(
            "openclaw-cax41.tail465e59.ts.net.:9120", "0.0.0.0"
        )
        assert not _is_accepted_host("attacker.example", "0.0.0.0")

    def test_wildcard_bind_uses_config_yaml_allowlist(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.web_server import _is_accepted_host

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "dashboard:\n"
            "  allowed_hosts:\n"
            "    - config-only.example\n"
            "    - 100.115.1.128\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)

        assert _is_accepted_host("config-only.example:9120", "0.0.0.0")
        assert _is_accepted_host("100.115.1.128:9120", "0.0.0.0")
        assert not _is_accepted_host("attacker.example", "0.0.0.0")

    def test_environment_allowlist_overrides_config_yaml(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.web_server import _is_accepted_host

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "dashboard:\n"
            "  allowed_hosts:\n"
            "    - config-only.example\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv(
            "HERMES_DASHBOARD_ALLOWED_HOSTS", "env-only.example"
        )

        assert _is_accepted_host("env-only.example", "0.0.0.0")
        assert not _is_accepted_host("config-only.example", "0.0.0.0")

    def test_malformed_environment_allowlist_fails_closed(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.web_server import _is_accepted_host

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "dashboard:\n"
            "  allowed_hosts:\n"
            "    - config-only.example\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HERMES_DASHBOARD_ALLOWED_HOSTS", " , ")

        assert not _is_accepted_host("config-only.example", "0.0.0.0")
        assert not _is_accepted_host("attacker.example", "0.0.0.0")

    def test_explicit_non_loopback_bind_requires_exact_match(self):
        """If the operator bound to a specific non-loopback hostname,
        the Host header must match exactly."""
        from hermes_cli.web_server import _is_accepted_host

        assert _is_accepted_host("my-server.corp.net", "my-server.corp.net")
        assert _is_accepted_host("my-server.corp.net:9119", "my-server.corp.net")
        # Different host — reject
        assert not _is_accepted_host("evil.example", "my-server.corp.net")
        # Loopback — reject (we bound to a specific non-loopback name)
        assert not _is_accepted_host("localhost", "my-server.corp.net")



class TestHostHeaderMiddleware:
    """End-to-end test via the FastAPI app — verify the middleware
    rejects bad Host headers with 400."""

    def test_rebinding_request_rejected(self):
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        # Simulate start_server having set the bound_host
        app.state.bound_host = "127.0.0.1"
        try:
            client = TestClient(app)
            # The TestClient sends Host: testserver by default — which is
            # NOT a loopback alias, so the middleware must reject it.
            resp = client.get(
                "/api/status",
                headers={"Host": "evil.example"},
            )
            assert resp.status_code == 400
            assert "Invalid Host header" in resp.json()["detail"]
        finally:
            # Clean up so other tests don't inherit the bound_host
            if hasattr(app.state, "bound_host"):
                del app.state.bound_host


    def test_no_bound_host_skips_validation(self):
        """If app.state.bound_host isn't set (e.g. running under test
        infra without calling start_server), middleware must pass through
        rather than crash."""
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        # Make sure bound_host isn't set
        if hasattr(app.state, "bound_host"):
            del app.state.bound_host

        client = TestClient(app)
        resp = client.get("/api/status")
        # Should get through to the status endpoint, not a 400
        assert resp.status_code != 400

    def test_dashboard_responses_include_browser_security_headers(self):
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        if hasattr(app.state, "bound_host"):
            del app.state.bound_host

        response = TestClient(app).get("/api/status")

        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-XSS-Protection"] == "0"

    def test_malformed_config_allowlist_fails_closed_through_middleware(
        self, tmp_path, monkeypatch
    ):
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "dashboard:\n"
            "  allowed_hosts:\n"
            "    host: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)
        app.state.bound_host = "0.0.0.0"
        try:
            response = TestClient(app).get(
                "/api/status", headers={"Host": "attacker.example"}
            )
            assert response.status_code == 400
            assert response.json()["detail"].startswith("Invalid Host header")
        finally:
            if hasattr(app.state, "bound_host"):
                del app.state.bound_host

    @pytest.mark.parametrize(
        "config_text",
        ["dashboard: invalid\n", "dashboard: []\n"],
    )
    def test_malformed_user_dashboard_section_fails_closed_through_middleware(
        self, tmp_path, monkeypatch, config_text
    ):
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(config_text, encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)
        app.state.bound_host = "0.0.0.0"
        try:
            response = TestClient(app).get(
                "/api/status", headers={"Host": "attacker.example"}
            )
            assert response.status_code == 400
            assert response.json()["detail"].startswith("Invalid Host header")
        finally:
            if hasattr(app.state, "bound_host"):
                del app.state.bound_host

    def test_malformed_config_file_fails_closed_through_middleware(
        self, tmp_path, monkeypatch
    ):
        """A fresh process must not turn a YAML parse error into allow-any."""
        from fastapi.testclient import TestClient

        from hermes_cli.config import load_config_readonly
        from hermes_cli.web_server import app

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "dashboard:\n  allowed_hosts: [unterminated\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)

        # Reproduce the dangerous ordering too: an ordinary caller first
        # caches the fresh-process defaults after the shared loader swallows
        # the parse failure. The strict Host boundary must not trust that
        # fallback cache entry.
        load_config_readonly()

        app.state.bound_host = "0.0.0.0"
        try:
            response = TestClient(app).get(
                "/api/status", headers={"Host": "attacker.example"}
            )
            assert response.status_code == 400
            assert response.json()["detail"].startswith("Invalid Host header")
        finally:
            if hasattr(app.state, "bound_host"):
                del app.state.bound_host

    def test_managed_config_allowlist_applies_through_middleware(
        self, tmp_path, monkeypatch
    ):
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("{}\n", encoding="utf-8")
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "config.yaml").write_text(
            "dashboard:\n  allowed_hosts:\n    - managed.example\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)

        app.state.bound_host = "0.0.0.0"
        try:
            client = TestClient(app)
            allowed = client.get(
                "/api/status", headers={"Host": "managed.example"}
            )
            denied = client.get(
                "/api/status", headers={"Host": "attacker.example"}
            )
            assert allowed.status_code != 400
            assert denied.status_code == 400
        finally:
            if hasattr(app.state, "bound_host"):
                del app.state.bound_host

    def test_malformed_managed_config_fails_closed_through_middleware(
        self, tmp_path, monkeypatch
    ):
        from fastapi.testclient import TestClient

        from hermes_cli.config import load_config_readonly
        from hermes_cli.web_server import app

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("{}\n", encoding="utf-8")
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "config.yaml").write_text(
            "dashboard:\n  allowed_hosts: [unterminated\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)

        # A normal reader keeps general startup fail-open. Its cache entry must
        # still carry the failed managed-parse signal for the strict boundary.
        load_config_readonly()

        app.state.bound_host = "0.0.0.0"
        try:
            response = TestClient(app).get(
                "/api/status", headers={"Host": "attacker.example"}
            )
            assert response.status_code == 400
            assert response.json()["detail"].startswith("Invalid Host header")
        finally:
            if hasattr(app.state, "bound_host"):
                del app.state.bound_host

    @pytest.mark.parametrize(
        "config_text",
        ["dashboard: invalid\n", "dashboard: []\n"],
    )
    def test_malformed_managed_dashboard_section_fails_closed_through_middleware(
        self, tmp_path, monkeypatch, config_text
    ):
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("{}\n", encoding="utf-8")
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "config.yaml").write_text(config_text, encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)
        app.state.bound_host = "0.0.0.0"
        try:
            response = TestClient(app).get(
                "/api/status", headers={"Host": "attacker.example"}
            )
            assert response.status_code == 400
            assert response.json()["detail"].startswith("Invalid Host header")
        finally:
            if hasattr(app.state, "bound_host"):
                del app.state.bound_host

    @pytest.mark.parametrize(
        "user_config_text",
        ["dashboard: invalid\n", "dashboard: []\n"],
    )
    def test_managed_overlay_cannot_hide_malformed_user_dashboard_section(
        self, tmp_path, monkeypatch, user_config_text
    ):
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            user_config_text, encoding="utf-8"
        )
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "config.yaml").write_text(
            "dashboard:\n  theme: managed-theme\n", encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)
        app.state.bound_host = "0.0.0.0"
        try:
            response = TestClient(app).get(
                "/api/status", headers={"Host": "attacker.example"}
            )
            assert response.status_code == 400
            assert response.json()["detail"].startswith("Invalid Host header")
        finally:
            if hasattr(app.state, "bound_host"):
                del app.state.bound_host

    @pytest.mark.parametrize(
        "user_config_text", ["[]\n", "false\n", "0\n", "null\n"]
    )
    def test_managed_overlay_cannot_hide_falsy_non_mapping_user_config_root(
        self, tmp_path, monkeypatch, user_config_text
    ):
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(user_config_text, encoding="utf-8")
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "config.yaml").write_text(
            "dashboard:\n  theme: managed-theme\n", encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)
        app.state.bound_host = "0.0.0.0"
        try:
            response = TestClient(app).get(
                "/api/status", headers={"Host": "attacker.example"}
            )
            assert response.status_code == 400
            assert response.json()["detail"].startswith("Invalid Host header")
        finally:
            if hasattr(app.state, "bound_host"):
                del app.state.bound_host

    @pytest.mark.parametrize(
        "managed_config_text", ["[]\n", "false\n", "0\n", "null\n"]
    )
    def test_falsy_non_mapping_managed_config_root_fails_closed(
        self, tmp_path, monkeypatch, managed_config_text
    ):
        from fastapi.testclient import TestClient
        from hermes_cli.web_server import app

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("{}\n", encoding="utf-8")
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "config.yaml").write_text(
            managed_config_text, encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)
        app.state.bound_host = "0.0.0.0"
        try:
            response = TestClient(app).get(
                "/api/status", headers={"Host": "attacker.example"}
            )
            assert response.status_code == 400
            assert response.json()["detail"].startswith("Invalid Host header")
        finally:
            if hasattr(app.state, "bound_host"):
                del app.state.bound_host

    def test_managed_dashboard_sibling_preserves_user_allowlist(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.web_server import _is_accepted_host

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "dashboard:\n  allowed_hosts:\n    - user.example\n",
            encoding="utf-8",
        )
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "config.yaml").write_text(
            "dashboard:\n  theme: managed-theme\n", encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)

        assert _is_accepted_host("user.example", "0.0.0.0")
        assert not _is_accepted_host("attacker.example", "0.0.0.0")

    def test_managed_allowlist_overrides_user_allowlist(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli.web_server import _is_accepted_host

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "dashboard:\n  allowed_hosts:\n    - user.example\n",
            encoding="utf-8",
        )
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (managed_dir / "config.yaml").write_text(
            "dashboard:\n  allowed_hosts:\n    - managed.example\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)

        assert _is_accepted_host("managed.example", "0.0.0.0")
        assert not _is_accepted_host("user.example", "0.0.0.0")


class TestWebSocketHostOriginGuard:
    """WebSocket upgrades must enforce the same dashboard boundary as HTTP."""

    def test_rebinding_websocket_host_is_rejected(self, monkeypatch):
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws.app.state, "bound_host", "127.0.0.1", raising=False)
        monkeypatch.setattr(ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)

        client = TestClient(ws.app)
        url = f"/api/events?token={ws._SESSION_TOKEN}&channel=security-test"
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                url,
                headers={
                    "Host": "evil.example",
                    "Origin": "http://evil.example",
                },
            ):
                pass

        assert exc.value.code == 4403

    @pytest.mark.parametrize("null_source", ["user", "managed"])
    def test_explicit_null_config_root_rejects_websocket(
        self, tmp_path, monkeypatch, null_source
    ):
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        import hermes_cli.web_server as ws

        hermes_home = tmp_path / "hermes-home"
        hermes_home.mkdir()
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        (hermes_home / "config.yaml").write_text(
            "null\n" if null_source == "user" else "{}\n", encoding="utf-8"
        )
        (managed_dir / "config.yaml").write_text(
            "null\n"
            if null_source == "managed"
            else "dashboard:\n  theme: managed-theme\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
        monkeypatch.delenv("HERMES_DASHBOARD_ALLOWED_HOSTS", raising=False)
        monkeypatch.setattr(ws.app.state, "bound_host", "0.0.0.0", raising=False)
        monkeypatch.setattr(ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)

        client = TestClient(ws.app)
        url = f"/api/events?token={ws._SESSION_TOKEN}&channel=null-root-test"
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                url,
                headers={
                    "Host": "attacker.example",
                    "Origin": "http://attacker.example",
                },
            ):
                pass
        assert exc.value.code == 4403


    def test_loopback_websocket_host_and_origin_are_accepted(self, monkeypatch):
        from fastapi.testclient import TestClient

        import hermes_cli.web_server as ws

        monkeypatch.setattr(ws.app.state, "bound_host", "127.0.0.1", raising=False)
        monkeypatch.setattr(ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)

        client = TestClient(ws.app)
        url = f"/api/events?token={ws._SESSION_TOKEN}&channel=security-test"
        with client.websocket_connect(
            url,
            headers={
                "Host": "localhost:9119",
                "Origin": "http://localhost:9119",
            },
        ):
            pass
