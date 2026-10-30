import json
import logging

from agent_harness.logging import log_event, setup_logging


def test_jsonl_redacts_secret_values_and_preserves_context(tmp_path):
    path = setup_logging(workspace_dir=str(tmp_path / "workspace"))
    log_event(
        logging.getLogger("redaction-test"), "llm_call", "model completed",
        session_id="session-1", run_id="run-1",
        llm_input='{"api_key":"sk-test-0123456789abcdef"}',
    )
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert "sk-test-0123456789abcdef" not in json.dumps(entry)
    assert entry["session_id(会话ID)"] == "session-1"
    assert entry["run_id(运行ID)"] == "run-1"


def test_jsonl_redacts_nested_values_pem_and_bare_key(tmp_path):
    path = setup_logging(workspace_dir=str(tmp_path / "workspace"))
    log_event(
        logging.getLogger("redaction-test"), "error", "failed",
        details={"password": "fake-password", "nested": [
            "sk-test-0123456789abcdef",
            "-----BEGIN PRIVATE KEY-----\nFAKE_PRIVATE_MATERIAL\n-----END PRIVATE KEY-----",
            "ordinary detail",
        ]},
    )
    text = path.read_text(encoding="utf-8")
    assert "fake-password" not in text
    assert "sk-test-0123456789abcdef" not in text
    assert "FAKE_PRIVATE_MATERIAL" not in text
    assert "ordinary detail" in text


def test_jsonl_redacts_escaped_json_and_incomplete_pem(tmp_path):
    path = setup_logging(workspace_dir=str(tmp_path / "workspace"))
    log_event(
        logging.getLogger("redaction-test"), "error", "failed",
        details='{"password":"fake\\\"password"} and '
                '-----BEGIN PRIVATE KEY-----\nPARTIAL_PRIVATE_MATERIAL',
    )
    text = path.read_text(encoding="utf-8")
    assert "fake" not in json.loads(text)["details"]
    assert "PARTIAL_PRIVATE_MATERIAL" not in text


def test_jsonl_redacts_oauth_authentication_and_cli_arguments(tmp_path):
    path = setup_logging(workspace_dir=str(tmp_path / "workspace"))
    log_event(
        logging.getLogger("redaction-test"), "error", "failed",
        details={"access_token": "fake_access_123", "client_secret": "fake_client_456",
                 "message": "Authorization: Basic fake_basic_789; --api-key fake_cli_123",
                 "json": '{"refresh_token":"fake_refresh_123"}',
                 "quoted": '--client-secret "fake secret words"'},
    )
    text = path.read_text(encoding="utf-8")
    assert all(value not in text for value in (
        "fake_access_123", "fake_client_456", "fake_basic_789", "fake_cli_123",
        "fake_refresh_123", "fake secret words",
    ))
