"""#203 / ADR-0032：自定义模型供应商管理（核心层：ProviderStore + 连接测试）。

钉 ADR-0032 §10 的 T1/T3/T4/T5/T9/T10。密钥用 `keyring` 的 fail backend /
内存注入验证生命周期；**任何真实 key 不得出现在本文件**（假 key 全部
`sk-test` 前缀）。测试断言沿用 ADR-0032 §7.2 的防回归形状。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.config import Settings
from agent_harness.model.provider_store import (
    CredentialError,
    MemoryCredentialStore,
    ProviderStore,
    SystemCredentialStore,
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


# ── #215：无 key 的供应商必须能删掉；keyring **真故障**仍必须中止 ─────
#
# 这两条锁的是同一行代码的两侧（`SystemCredentialStore.delete` 的异常分流）：
# keyring 用 `PasswordDeleteError` 表达"这条凭据不存在"，把它并进 KeyringError
# 会让 `ProviderStore.delete`（D6：先删凭据、异常即中止）在**无 key 的供应商**
# 上永远中止——而"无 key"正是新建供应商的默认状态（#215 的实测现象）。
# 只用 monkeypatch 换掉 keyring 那一次调用，不碰 store 的其余真实对象，
# 因此判据与"本机 keyring 后端是否可用"无关。


def test_delete_keyless_provider_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import keyring
    from keyring.errors import PasswordDeleteError

    def _missing(service: str, provider_id: str) -> None:
        raise PasswordDeleteError("no such password")

    monkeypatch.setattr(keyring, "delete_password", _missing)
    store = ProviderStore(tmp_path / "p.json", SystemCredentialStore())
    store.create(_body(api_key=None))  # 不填 key（= 新建供应商的默认状态）
    store.delete("my-proxy")
    assert store.list_entries() == []


def test_delete_aborts_when_keyring_backend_fails(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch):
    import keyring
    from keyring.errors import KeyringError

    def _broken(service: str, provider_id: str) -> None:
        raise KeyringError("backend down")

    monkeypatch.setattr(keyring, "delete_password", _broken)
    store = ProviderStore(tmp_path / "p.json", SystemCredentialStore())
    store.create(_body())  # 带 key：凭据确实存在，删不掉就不能删配置
    with pytest.raises(CredentialError):
        store.delete("my-proxy")
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


# ── T4 补：models 并集（覆盖内置 §3.2） ──────────────────────────────


def test_override_builtin_merges_models_union(store: ProviderStore):
    """同 id 覆盖内置 ⇒ models 并集（自定义优先，内置默认模型保留）。"""
    store.create(_body(id="deepseek", base_url="https://my-proxy.internal/v1",
                       models=[{"model_id": "my-model"}]))
    entry = store.get_entry("deepseek")
    model_ids = [m["model_id"] for m in entry["models"]]
    # 自定义优先在前列，内置默认模型 deepseek-chat 保留（并集）。
    assert model_ids == ["my-model", "deepseek-chat"]


def test_new_custom_provider_keeps_models_exactly(store: ProviderStore):
    """新 id（不覆盖内置）⇒ models 原样（无内置并集）。"""
    store.create(_body(models=[{"model_id": "only-mine"}]))
    entry = store.get_entry("my-proxy")
    assert [m["model_id"] for m in entry["models"]] == ["only-mine"]


class TestResolveSelectionChain:
    """终审 P1 修复：统一解析点 `ModelConfig.resolve_selection`——
    /api/models 广告的自定义条目（`<provider>:<model_id>`）在 create/resume/
    model 三道校验闸此前被 from_catalog 422（UI 能选、一提交就拒，feature
    promise 断裂）。三道闸与 build_runtime 引用本函数后，广告列表与可解析
    集合同一。"""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        """MemoryCredentialStore 注入（与 provider_app fixture 同一口径：
        不碰真实系统凭据管理器）。"""
        from agent_harness.model.provider_store import (
            MemoryCredentialStore,
            ProviderStore,
        )

        return ProviderStore(
            tmp_path / "model-providers.json",
            MemoryCredentialStore(),
            builtin_ids=frozenset({"deepseek"}),
        )

    def test_catalog_name_still_resolves_via_catalog(self, store):
        """无冒号的名字仍按 catalog 解析（既有契约不变）。"""
        from agent_harness.model.config import ModelConfig

        catalog = '[{"name": "gpt-4o", "provider": "deepseek", "model_name": "gpt-4o-mini"}]'
        settings = Settings(_env_file=None, model_api_key="sk-test",
                            model_provider="deepseek", model_name="deepseek-chat",
                            agent_models=catalog)
        resolved = ModelConfig.resolve_selection(settings, "gpt-4o", store)
        assert resolved.provider == "deepseek"  # catalog 条目的 provider

    def test_composite_id_resolves_custom_provider(self, store):
        """`<provider>:<model_id>` 命名空间 → from_custom_provider（凭据在场）。"""
        from agent_harness.model.config import ModelConfig

        store.create({"id": "my-proxy", "label": "My Proxy",
                      "base_url": "https://my-proxy.internal/v1",
                      "models": [{"model_id": "gpt-x"}],
                      "api_key": "sk-custom-123"})
        settings = Settings(_env_file=None, model_api_key="sk-test",
                            model_provider="deepseek", model_name="deepseek-chat")
        resolved = ModelConfig.resolve_selection(settings, "my-proxy:gpt-x", store)
        assert resolved.provider == "my-proxy"
        assert resolved.model_name == "gpt-x"

    def test_composite_id_deleted_provider_is_loud(self, store):
        """被删 provider 的 composite id **不静默 fallback**（D9）——响亮失败。"""
        import pytest

        from agent_harness.model.config import ConfigError, ModelConfig

        settings = Settings(_env_file=None, model_api_key="sk-test",
                            model_provider="deepseek", model_name="deepseek-chat")
        with pytest.raises(ConfigError, match="my-proxy"):
            ModelConfig.resolve_selection(settings, "my-proxy:gpt-x", store)

    def test_no_colon_in_catalog_fails_loud(self, store):
        """未知 catalog 名 → 未知模型错误（既有 422 语义不变）。"""
        import pytest

        from agent_harness.model.config import ConfigError, ModelConfig

        settings = Settings(_env_file=None, model_api_key="sk-test",
                            model_provider="deepseek", model_name="deepseek-chat")
        with pytest.raises(ConfigError, match="未知模型"):
            ModelConfig.resolve_selection(settings, "no-such-model", store)

    def test_store_none_composite_id_fails_loud(self):
        """store 不在场（CLI 等未接 provider_store 的调用方）+ composite id
        → 确定性错误，不假装是 catalog 问题。"""
        import pytest

        from agent_harness.model.config import ConfigError, ModelConfig

        settings = Settings(_env_file=None, model_api_key="sk-test",
                            model_provider="deepseek", model_name="deepseek-chat")
        with pytest.raises(ConfigError, match="供应商存储"):
            ModelConfig.resolve_selection(settings, "my-proxy:gpt-x", None)
