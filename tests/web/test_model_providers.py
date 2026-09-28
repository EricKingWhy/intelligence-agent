"""#203 / ADR-0032：model-providers CRUD + 测试连接的 web 层契约。

钉 ADR-0032 §10 的 T2（无密钥回显）/ T6（测试走真实构造）/ T7（失败归类）/
T8（scheme 422）/ T11（被删 provider 不静默 fallback）。密钥用假 key
（sk-test 前缀）；**任何真实 key 不得出现在本文件**。
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.model.provider_store import MemoryCredentialStore, ProviderStore
from agent_harness.web.app import create_app


@pytest.fixture
def provider_app(tmp_path):
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        model_provider="deepseek", model_name="deepseek-chat",
        provider_store_path=str(tmp_path / "model-providers.json"),
    )
    app = create_app(settings, enable_cors=False)
    # 测试注入内存凭据后端：SystemCredentialStore 在 Windows 上就是真实凭据
    # 管理器——测试 key 会写进系统级存储并在用例间泄漏（实测 my-proxy 残留）。
    # keyring 注入（memory backend）是 ADR-0032 §10 T3 的既有口径。
    from agent_harness.model.provider_store import MemoryCredentialStore

    app.state.agent.provider_store.credentials = MemoryCredentialStore()
    return app


@pytest.fixture
def client(provider_app):
    return TestClient(provider_app)


def _body(**overrides):
    body = {
        "id": "my-proxy",
        "label": "自建代理",
        "base_url": "https://api.example.com/v1",
        "models": [{"model_id": "deepseek-chat"}],
        "api_key": "sk-test-not-a-real-key",
    }
    body.update(overrides)
    return body


# ── T1（web 层）：CRUD 端点 ──────────────────────────────────────────


class TestCrudEndpoints:
    def test_create_list_update_delete(self, client):
        resp = client.post("/api/model-providers", json=_body())
        assert resp.status_code in (200, 201)
        assert resp.json()["kind"] == "custom"

        resp = client.get("/api/model-providers")
        assert resp.status_code == 200
        [entry] = resp.json()["providers"]
        assert entry["id"] == "my-proxy"
        assert entry["has_api_key"] is True
        assert entry["is_available"] is True

        resp = client.put("/api/model-providers/my-proxy", json={"label": "改名"})
        assert resp.status_code == 200
        assert resp.json()["label"] == "改名"

        resp = client.delete("/api/model-providers/my-proxy")
        assert resp.status_code == 200
        assert client.get("/api/model-providers").json()["providers"] == []

    def test_delete_missing_is_404(self, client):
        assert client.delete("/api/model-providers/ghost").status_code == 404

    def test_delete_keyless_provider_succeeds(self, tmp_path, monkeypatch):
        """#215 回归锁（**在端点这一层**，因为它就是在端点上被报出来的）。

        ⚠ 必须用**真实适配器** `SystemCredentialStore`：把 `PasswordDeleteError`
        转成"删除已满足"（读回确认）的逻辑只住在它里面。夹具默认注入的内存凭据后端
        的 `delete` 对不存在的条目是宽容的——这正是这条 bug 从既有夹具里溜过去的原因
        （自造一个 Memory 子类也不行：那等于绕过被改的那一层）。

        只 monkeypatch keyring 的三个函数，**不碰真实凭据管理器**：`set_password`
        直接抛错 ⇒ 任何"偷偷写系统级存储"的回归当场红（本批 REVIEW 时真的踩到过
        一次 sk-test 残留）。
        """
        import keyring
        from keyring.errors import PasswordDeleteError

        def _no_write(service, provider_id, password):
            raise AssertionError("用例不得向真实凭据后端写入任何东西")

        def _delete_raises(service, provider_id):
            # WinVault 的形态：删一条不存在的凭据抛 PasswordDeleteError
            raise PasswordDeleteError("no such password")

        monkeypatch.setattr(keyring, "set_password", _no_write)
        monkeypatch.setattr(keyring, "delete_password", _delete_raises)
        monkeypatch.setattr(keyring, "get_password", lambda service, provider_id: None)

        settings = Settings(
            _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
            model_provider="deepseek", model_name="deepseek-chat",
            provider_store_path=str(tmp_path / "model-providers.json"),
        )
        app = create_app(settings, enable_cors=False)
        assert type(app.state.agent.provider_store.credentials).__name__ == "SystemCredentialStore"
        client = TestClient(app)

        assert client.post("/api/model-providers", json=_body(api_key=None)).status_code in (200, 201)
        resp = client.delete("/api/model-providers/my-proxy")
        assert resp.status_code == 200, resp.text
        assert client.get("/api/model-providers").json()["providers"] == []
        # 残留会污染目录（#215 的实测现象：3 条内置目录断言变红）——这里顺带锁上
        assert not any("my-proxy" in m["name"] for m in client.get("/api/models").json()["models"])


# ── T2：无密钥回显 ───────────────────────────────────────────────────


class TestNoSecretLeak:
    def test_endpoints_never_return_api_key(self, client):
        client.post("/api/model-providers", json=_body())
        for path in ("/api/model-providers", "/api/models"):
            body = client.get(path).json()
            serialized = json.dumps(body)
            # has_api_key 是 ADR-0032 §4 契约的**状态字段**（不含任何 key 值）；
            # 其余任何 api_key 值字段与 sk- 子串都必须缺席。
            for model in body.get("models", []):
                assert "api_key" not in model, f"{path} 的模型条目不得携带 api_key"
            for provider in body.get("providers", []):
                assert "api_key" not in provider, f"{path} 不得携带 api_key 值字段"
                assert set(provider) & {"api_key"} == set()
            assert "sk-test" not in serialized, f"{path} 不得携带任何 key 子串"

    def test_put_without_key_never_echoes(self, client):
        client.post("/api/model-providers", json=_body())
        resp = client.put("/api/model-providers/my-proxy",
                          json={"base_url": "https://changed.example.com/v1"})
        assert resp.status_code == 200
        assert "sk-" not in json.dumps(resp.json())


# ── T8：scheme 校验 ──────────────────────────────────────────────────


class TestValidation:
    def test_file_scheme_is_422(self, client):
        resp = client.post("/api/model-providers", json=_body(base_url="file:///etc/passwd"))
        assert resp.status_code == 422

    def test_bad_id_is_422(self, client):
        resp = client.post("/api/model-providers", json=_body(id="Bad Id"))
        assert resp.status_code == 422

    def test_empty_models_is_422(self, client):
        resp = client.post("/api/model-providers", json=_body(models=[]))
        assert resp.status_code == 422


# ── T6：连接测试走真实构造路径 ───────────────────────────────────────


class TestConnectionTest:
    def test_test_uses_create_chat_model_with_resolved_config(self, client):
        resp = client.post("/api/model-providers", json=_body())
        assert resp.status_code in (200, 201)

        captured: dict = {}

        invoke_kwargs: dict = {}

        class _FakeModel:
            async def ainvoke(self, messages, **kwargs):
                invoke_kwargs.update(kwargs)
                return "ok"

        def _capture(config, **kwargs):
            # 模拟真实 create_chat_model：从 ModelConfig 取明文（get_secret_value
            # 是取明文的唯一出口——captured 直接持 SecretStr 会假绿）。
            captured["base_url"] = config.base_url
            captured["api_key"] = config.get_secret_value()
            captured["model"] = config.model_name
            captured.update(kwargs)
            return _FakeModel()

        with patch("agent_harness.web.model_providers.create_chat_model",
                   side_effect=_capture):
            resp = client.post("/api/model-providers/my-proxy/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        # 真实构造：base_url/key/model 从解析后的配置来；tools 未传、max_tokens=1。
        assert captured["base_url"] == "https://api.example.com/v1"
        assert captured["api_key"] == "sk-test-not-a-real-key"
        assert captured["model"] == "deepseek-chat"
        assert "tools" not in captured
        # max_tokens 是 ainvoke 调用参数（§6.2 固定参数之一）。
        assert invoke_kwargs.get("max_tokens") == 1
        # 结果写回 last_test（非密）。
        entry = client.get("/api/model-providers").json()["providers"][0]
        assert entry["last_test"]["ok"] is True
        assert "sk-" not in json.dumps(entry)

    def test_test_failure_is_classified_and_redacted(self, client):
        client.post("/api/model-providers", json=_body())

        class _FailingModel:
            def __init__(self, **kwargs):
                pass

            async def ainvoke(self, messages, **kwargs):
                raise RuntimeError("401 unauthorized with sk-test-not-a-real-key")

        with patch("agent_harness.web.model_providers.create_chat_model",
                   return_value=_FailingModel()):
            resp = client.post("/api/model-providers/my-proxy/test")
        body = resp.json()
        assert body["ok"] is False
        # 失败归类（网络层异常 → network_error）。
        assert body["reason"] in ("network_error", "auth_failed", "provider_error")
        # 摘要截断 ≤500 且不含 key 子串。
        assert "sk-test" not in body["detail"]
        assert len(body["detail"]) <= 500

    def test_test_missing_provider_is_404(self, client):
        assert client.post("/api/model-providers/ghost/test").status_code == 404

    def test_test_without_credentials_is_rejected(self, client):
        # 无 key 的 provider：测试连接应给出可读失败，不是 500。
        client.post("/api/model-providers", json=_body(api_key=None))
        resp = client.post("/api/model-providers/my-proxy/test")
        assert resp.status_code in (400, 409, 422)


# ── T11：被删 provider 不静默 fallback ───────────────────────────────


class TestDeletedProviderResolution:
    def test_session_model_resolution_fails_loudly_after_delete(self, client, tmp_path):
        """被删 provider 的自定义模型：resolve 路径必须明确失败（不静默 fallback）。

        通过 ProviderStore 层直接验证（web 端点只做 CRUD；会话解析走
        parse_provider_catalog 的合并视图）。"""
        from agent_harness.model.provider_store import resolve_provider_target

        store = ProviderStore(tmp_path / "model-providers.json", MemoryCredentialStore(),
                              builtin_ids=frozenset({"deepseek"}))
        store.create(_body(models=[{"model_id": "deepseek-chat"}],
                           label="自建代理"))
        resolved = resolve_provider_target(store, settings_provider="my-proxy",
                                           model_id="deepseek-chat")
        assert resolved is not None
        assert resolved["base_url"] == "https://api.example.com/v1"

        store.delete("my-proxy")
        resolved = resolve_provider_target(store, settings_provider="my-proxy",
                                           model_id="deepseek-chat")
        # 被删 provider：返回 None（调用方给明确错误），不静默 fallback。
        assert resolved is None
