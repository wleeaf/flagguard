"""Deployment/runtime hardening regression tests."""

from pathlib import Path

import pytest

import config
from panel import cli as panel_cli


def _set_minimal_valid_config(monkeypatch, tmp_path: Path) -> None:
    sa = tmp_path / "sa.json"
    sa.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "fake-token")
    monkeypatch.setattr(config, "CHALLENGE_FLAG", f"{config.FLAG_PREFIX}{{x}}")
    monkeypatch.setattr(config, "SERVICE_ACCOUNT_PATH", str(sa))
    monkeypatch.setattr(config, "BOT_MODE", "polling")
    monkeypatch.setattr(config, "WEBHOOK_URL", "")
    monkeypatch.setattr(config, "WEBHOOK_PATH", "/webhook")
    monkeypatch.setattr(config, "WEBHOOK_PORT", 8443)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://localhost/aishield")
    monkeypatch.setattr(config, "DATABASE_POOL_MIN_SIZE", 1)
    monkeypatch.setattr(config, "DATABASE_POOL_MAX_SIZE", 2)
    monkeypatch.setattr(config, "RATE_LIMIT_BACKEND", "auto")
    monkeypatch.setattr(config, "APP_ENV", "development")


def test_validate_required_config_requires_webhook_url(monkeypatch, tmp_path):
    _set_minimal_valid_config(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "BOT_MODE", "webhook")

    with pytest.raises(RuntimeError, match="WEBHOOK_URL is required"):
        config.validate_required_config()


def test_validate_required_config_requires_database_url(monkeypatch, tmp_path):
    _set_minimal_valid_config(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "DATABASE_URL", "")

    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        config.validate_required_config()


def test_panel_cli_run_server_disables_reload(monkeypatch):
    called = {}

    def fake_uvicorn_run(*args, **kwargs):
        called["reload"] = kwargs.get("reload")
        called["host"] = kwargs.get("host")
        called["port"] = kwargs.get("port")

    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)
    monkeypatch.setattr("config.validate_panel_config", lambda: None)
    monkeypatch.setattr("config.PANEL_HOST", "127.0.0.1")
    monkeypatch.setattr("config.PANEL_PORT", 9000)

    panel_cli.run_server()

    assert called["reload"] is False
    assert called["host"] == "127.0.0.1"
    assert called["port"] == 9000
