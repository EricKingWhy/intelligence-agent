"""#203 / ADR-0032：自定义模型供应商管理（核心层：ProviderStore + 连接测试）。

钉 ADR-0032 §10 的 T1/T3/T4/T5/T9/T10。密钥用 `keyring` 的 fail backend /
内存注入验证生命周期；**任何真实 key 不得出现在本文件**（假 key 全部
`sk-test` 前缀）。测试断言沿用 ADR-0032 §7.2 的防回归形状。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.model.provider_store import (
    CredentialError,
    MemoryCredentialStore,
    ProviderStore,
    validate_base_url,
    validate_provider_id,
)


@pytest.fixture
def store(tmp_path: Path) -> ProviderStore:
    return ProviderStore(tmp_path / "model-providers.json", MemoryCredentialStore(),
                         builtin_ids=frozenset({"deepseek", "qwen", "tencent"}))


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


# ── T1：CRUD 基础 ────────────────────────────────────────────────────


def test_crud_roundtrip(store: ProviderStore):
    store.create(_body())
    [entry] = store.list_entries()
    assert entry["id"] == "my-proxy"
    assert entry["kind"] == "custom"

    store.update("my-proxy", {"label": "改名"})
    [entry] = store.list_entries()
    assert entry["label"] == "改名"

    store.delete("my-proxy")
    assert store.list_entries() == []


def test_delete_missing_is_404(store: ProviderStore):
    with pytest.raises(KeyError):
        store.delete("ghost")


# ── T3：凭据生命周期（keyring 注入） ─────────────────────────────────


def test_credential_lifecycle(store: ProviderStore):
    creds: MemoryCredentialStore = store.credentials
    store.create(_body())
    assert creds.get("my-proxy") == "sk-test-not-a-real-key"

    # PUT 省略 api_key ⇒ 凭据不变。
    store.update("my-proxy", {"base_url": "https://changed.example.com/v1"})
    assert creds.get("my-proxy") == "sk-test-not-a-real-key"

    # PUT api_key="" ⇒ 显式清除凭据。
    store.update("my-proxy", {"api_key": ""})
    assert creds.get("my-proxy") is None

    # 重新写入再删除 ⇒ 凭据消失。
    store.update("my-proxy", {"api_key": "sk-test-2"})
    assert creds.get("my-proxy") == "sk-test-2"
    store.delete("my-proxy")
    assert creds.get("my-proxy") is None


def test_json_file_never_contains_secrets(store: ProviderStore, tmp_path: Path):
    """ADR-0032 §3.1：配置文件不含任何密钥字段（连哈希都不存）。"""
    store.create(_body())
    raw = (tmp_path / "model-providers.json").read_text(encoding="utf-8")
    assert "api_key" not in raw
    assert "sk-test" not in raw


# ── T4：覆盖内置 ─────────────────────────────────────────────────────


def test_override_builtin_provider(store: ProviderStore):
    store.create(_body(id="deepseek", base_url="https://my-proxy.internal/v1",
                       models=[{"model_id": "deepseek-chat"}]))
    [entry] = store.list_entries()
    assert entry["kind"] == "override"
    # base_url 生效（覆盖内置预设）。
    resolved = store.resolve_provider_config("deepseek")
    assert resolved["base_url"] == "https://my-proxy.internal/v1"


# ── T5：is_available 真实判定（不做网络探测） ────────────────────────


def test_is_available_is_local_credential_check(store: ProviderStore):
    store.create(_body())
    entry = store.get_entry("my-proxy")
    assert entry["is_available"] is True
    assert entry["unavailable_reason"] is None

    store.update("my-proxy", {"api_key": ""})
    entry = store.get_entry("my-proxy")
    assert entry["is_available"] is False
    assert entry["unavailable_reason"] == "missing_api_key"


# ── T8：scheme 校验 ──────────────────────────────────────────────────


def test_base_url_scheme_validation():
    assert validate_base_url("https://api.example.com/v1") is True
    assert validate_base_url("http://localhost:8000") is True
    assert validate_base_url("file:///etc/passwd") is False
    assert validate_base_url("ftp://x") is False
    assert validate_base_url("not-a-url") is False


def test_provider_id_slug_validation():
    assert validate_provider_id("my-proxy") is True
    assert validate_provider_id("a1_b-2") is True
    assert validate_provider_id("-bad") is False
    assert validate_provider_id("Bad") is False
    assert validate_provider_id("") is False
    assert validate_provider_id("x" * 65) is False


# ── T9：删除顺序——凭据删除失败 ⇒ 中止，配置保留 ─────────────────────


def test_delete_aborts_when_credential_delete_fails(store: ProviderStore):
    store.create(_body())

    class _Failing(MemoryCredentialStore):
        def delete(self, provider_id: str) -> None:
            raise CredentialError("win32 error 5")

    store.credentials = _Failing()
    with pytest.raises(CredentialError):
        store.delete("my-proxy")
    # 配置保持原样——不出现"配置没了凭据还在"。
    assert [e["id"] for e in store.list_entries()] == ["my-proxy"]


# ── T10：无可用凭据后端 ⇒ 带 key 的写入报错，不落明文 ────────────────


def test_unavailable_credential_backend_never_writes_plaintext(store: ProviderStore):
    class _Unavailable(MemoryCredentialStore):
        def available(self) -> bool:
            return False

        def set(self, provider_id: str, api_key: str) -> None:
            raise CredentialError("no persistent credential backend")

    store.credentials = _Unavailable()
    with pytest.raises(CredentialError):
        store.create(_body())
    # 配置也不落盘（带 key 的创建要么整体成功要么整体失败）。
    assert store.list_entries() == []


# ── 数据持久化（T1 的落盘半） ────────────────────────────────────────


def test_store_persists_across_instances(tmp_path: Path):
    path = tmp_path / "model-providers.json"
    first = ProviderStore(path, MemoryCredentialStore())
    first.create(_body())
    second = ProviderStore(path, MemoryCredentialStore())
    assert [e["id"] for e in second.list_entries()] == ["my-proxy"]
    # last_test 结构非密。
    assert "sk-" not in json.dumps(second.list_entries())
