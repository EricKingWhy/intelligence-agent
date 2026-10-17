"""HTTP contract coverage for the Watchtower rules API."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app

_RULES = "/api/watchtower/rules"
_CONSENT_VERSION = "watchtower-consent-v1"


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
    )
    return TestClient(create_app(settings, enable_cors=False))


def _body(**overrides) -> dict:
    body = {
        "prompt": "Check the nightly build and summarize failures",
        "trigger_kind": "relative",
        "timezone": "UTC",
        "delay_seconds": 3600,
        "local_time": None,
        "model": None,
        "sandbox_backend": "local",
        "cwd": None,
        "budget_turns": 4,
        "budget_tokens": 32000,
        "budget_duration_seconds": 1800,
        "consent_acknowledged": True,
        "consent_version": _CONSENT_VERSION,
    }
    return {**body, **overrides}


def test_create_requires_backend_consent_and_persists_rule(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        rejected = client.post(_RULES, json=_body(consent_acknowledged=False))
        assert rejected.status_code == 422

        rejected_version = client.post(
            _RULES, json=_body(consent_version="stale-consent-version"),
        )
        assert rejected_version.status_code == 422

        created = client.post(_RULES, json=_body())
        assert created.status_code == 200, created.text
        rule = created.json()["rule"]
        assert rule["consent_version"] == _CONSENT_VERSION
        assert rule["consented_at"]
        assert rule["sandbox_backend"] == "local"
        assert rule["budget_duration_seconds"] == 1800

        paused = client.post(f"{_RULES}/{rule['rule_id']}/pause")
        assert paused.status_code == 200, paused.text
        assert paused.json()["rule"]["state"] == "paused"

        resumed = client.post(f"{_RULES}/{rule['rule_id']}/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["rule"]["state"] == "active"

        snapshot = client.get("/api/watchtower")
        assert snapshot.status_code == 200, snapshot.text
        payload = snapshot.json()
        assert payload["scheduler"]["running"] is True
        assert payload["scheduler"]["single_instance"] is True
        assert payload["rules"][0]["rule_id"] == rule["rule_id"]
        assert payload["rules"][0]["consented_at"] == rule["consented_at"]
