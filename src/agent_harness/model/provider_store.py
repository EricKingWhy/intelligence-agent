"""自定义模型供应商存储（#203 / ADR-0032）：非密配置进本地 JSON，密钥只进凭据管理器。

决策（ADR-0032 §2）：
- D2：非密字段落本地 JSON；**该文件不含任何密钥字段**（连哈希都不存）。
- D3：密钥用 `keyring`（MIT，Windows 后端即凭据管理器）——REUSE 而非 BUILD。
- D6：删除 = 先删凭据（失败则中止）、再删配置——不产生"孤立可用密钥"。
- D10：凭据后端不可持久化时明确报错，绝不落明文。
- D7：`is_available` 是真实本地判定（有凭据 ⇒ true），语义是**已配置**而非
  "网络可达"——可达性由「测试连接」给结论；列表不做网络探测（不变量 #21）。

凭据坐标：`service = "agent-harness:model-provider"`，`username = <provider id>`。
本模块不 import keyring 的具体后端——真实后端由 `SystemCredentialStore` 在调用
时经 keyring 门面访问，测试注入 `MemoryCredentialStore`（替换 Fake Provider，
§9.4 Goal-Driven：加 Provider 至少有替换 Fake 的测试）。

与 `model/config.py` 的依赖方向：config → 本模块（`resolve_selection` 走
`from_custom_provider`），本模块 → config 的符号（PROVIDER_PRESETS）只在函数内
局部 import。契约是**不出现模块级互相 import**（会成环）；就近 import 是这条
单向依赖的落地方式，不是随手为之。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("agent_harness.model")

#: keyring service 坐标（ADR-0032 §5）。所有凭据共用一个 service，username = provider id。
CREDENTIAL_SERVICE = "agent-harness:model-provider"

#: provider id 的 slug 规则（ADR-0032 §4.1）。
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-_]{0,63}$")

#: base_url 允许的 scheme。**拒绝** file:/ftp: 等（file:// 会把本地文件读进请求）。
_ALLOWED_SCHEMES = {"http", "https"}


class CredentialError(Exception):
    """凭据操作失败（后端不可用 / Win32 错误 / 凭据被外部删除）。"""


class ProviderStoreError(Exception):
    """供应商配置操作失败（校验 / 形状 / 删除不存在）。"""


def validate_provider_id(provider_id: str) -> bool:
    """provider id 必须是 slug（`^[a-z0-9][a-z0-9-_]{0,63}$`，ADR-0032 §4.1）。"""
    return bool(_PROVIDER_ID_RE.match(provider_id))


def validate_base_url(base_url: str) -> bool:
    """base_url 必须 http/https；拒绝 file:/ftp: 与非 URL（§4.1）。"""
    try:
        parts = urlsplit(base_url)
    except ValueError:
        return False
    return parts.scheme in _ALLOWED_SCHEMES and bool(parts.netloc)


class Credentials(ABC):
    """凭据后端 seam（ADR-0032 §5）。真实后端走 keyring；测试注入内存实现。"""

    @abstractmethod
    def available(self) -> bool:
        """后端是否可持久化。False ⇒ 带 key 的写入必须失败（D10，绝不落明文）。"""

    @abstractmethod
    def set(self, provider_id: str, api_key: str) -> None:
        """写入凭据；失败抛 CredentialError。"""

    @abstractmethod
    def get(self, provider_id: str) -> str | None:
        """读取凭据；读不到（未配置 / 被外部删除 / 后端不可用）返回 None。"""

    @abstractmethod
    def delete(self, provider_id: str) -> None:
        """删除凭据；失败抛 CredentialError。"""


class MemoryCredentialStore(Credentials):
    """测试用内存凭据后端（零 keyring 依赖）。"""

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}

    def available(self) -> bool:
        return True

    def set(self, provider_id: str, api_key: str) -> None:
        self._credentials[provider_id] = api_key

    def get(self, provider_id: str) -> str | None:
        return self._credentials.get(provider_id)

    def delete(self, provider_id: str) -> None:
        self._credentials.pop(provider_id, None)


class SystemCredentialStore(Credentials):
    """真实凭据后端：keyring 门面（Windows 上即凭据管理器 WinVault）。

    keyring 落到不可持久化后端（无桌面会话的服务账户 / 未配置的 Linux）时
    `available()` 返回 False——写入端据此 503 拒绝（D10），**绝不**降级明文。
    """

    def available(self) -> bool:
        import keyring

        backend = keyring.get_keyring()
        # fail Keyring / chainer 无可持久化后端时 priority 最低且 set 会抛
        # NoKeyringError——按"不可用"上报，让写入端显式失败。
        priority = getattr(backend, "priority", 0)
        return not (
            type(backend).__name__ in {"fail", "ChainerDetector"}
            or (isinstance(priority, (int, float)) and priority <= 0)
        )

    def set(self, provider_id: str, api_key: str) -> None:
        import keyring
        from keyring.errors import KeyringError

        try:
            keyring.set_password(CREDENTIAL_SERVICE, provider_id, api_key)
        except KeyringError as error:
            raise CredentialError(f"凭据写入失败: {type(error).__name__}") from error

    def get(self, provider_id: str) -> str | None:
        import keyring
        from keyring.errors import KeyringError

        try:
            return keyring.get_password(CREDENTIAL_SERVICE, provider_id)
        except KeyringError:
            # 后端不可用 / 凭据被外部删除：读侧按"未配置"处理（诚实降级，
            # is_available=false + credential_unavailable），不拖垮其他 provider。
            logger.warning("凭据读取失败（provider=%s）", provider_id)
            return None

    def delete(self, provider_id: str) -> None:
        import keyring
        from keyring.errors import KeyringError

        try:
            keyring.delete_password(CREDENTIAL_SERVICE, provider_id)
        except KeyringError as error:
            # ⚠ **不能按异常类放行**：`PasswordDeleteError` 的库定义是"**删不掉**"
            # （`keyring/errors.py`：`Raised when the password can't be deleted`，
            # 基类 `backend.py::delete_password` 的"后端不支持删除"也走它）——后端拒绝、
            # 钥匙串删失败、kwallet 用户取消解锁都会落在这里；WinVault 只是**恰好**把
            # "本来就没有这条"也算进同一个异常。按类放行 = 在这些后端上把真失败当成功、
            # 接着删掉配置 ⇒ 正是 D6 要消灭的"孤立可用密钥"。
            #
            # 所以改为**读回确认**：凭据确实不在了才算"删除的语义已满足"（#215：新建
            # 供应商默认不带 key，那时必须能删掉）；还在 ⇒ 仍按 D6 中止。
            #
            # ⚠ 确认**不能走 `self.get()`**：读侧把后端故障降级成 None（诚实降级，见
            # 上面的 `get`），拿它当"凭据已不在"的判据就成了 fail-open——后端同时删不掉
            # 也读不到时（kwallet 取消解锁 / macOS 钥匙串锁定 ⇒ 两次调用都抛），配置被
            # 删而密钥还在，正是 D6 要消灭的"孤立可用密钥"。**读不出来 = 无法确认 =
            # 按失败处置**（中止是安全方向，与 D6 同向）。
            try:
                remaining = keyring.get_password(CREDENTIAL_SERVICE, provider_id)
            except KeyringError as read_error:
                raise CredentialError(f"凭据删除失败（无法确认）: {type(error).__name__}") from read_error
            if remaining is not None:
                raise CredentialError(f"凭据删除失败: {type(error).__name__}") from error


class ProviderStore:
    """自定义供应商的全局配置实体（ADR-0032 D1）。

    非密配置落本地 JSON；`api_key` 只存在于请求体（写路径）与凭据后端。
    同 id 覆盖内置 preset（`kind="override"`）；新 id 为 `kind="custom"`。
    """

    def __init__(self, path: Path, credentials: Credentials,
                 builtin_ids: frozenset[str] | set[str] = frozenset()) -> None:
        self._path = Path(path)
        self.credentials = credentials
        # 内置 preset id 集合（ADR-0032 §3.2 合并规则的"同 id = 覆盖内置"判据）。
        # 默认由 `for_settings` 填 PROVIDER_PRESETS；直接构造（测试）可显式指定。
        self._builtin_ids = frozenset(builtin_ids)

    @classmethod
    def for_settings(cls, settings: Any, credentials: Credentials | None = None) -> ProviderStore:
        """按 Settings 构造标准 store——**唯一构造入口**（ADR-0032）。

        `path` 取 `settings.provider_store_path`；`credentials` 缺省为真实
        keyring 后端（测试注入 `MemoryCredentialStore`）；`builtin_ids` 恒取内置
        preset 全集。

        此前四处调用点各写一遍 `ProviderStore(Path(settings.provider_store_path), …)`，
        且 `builtin_ids` 三种取值（全集 / 空集 / 省略）——同一份配置对象在不同路径
        上语义不同（`kind` 派生、`create()` 的 models 并集都读它）。收敛到这里，
        装配层与校验闸拿到的是**同一个** store 语义。
        """
        from agent_harness.model.config import PROVIDER_PRESETS

        if credentials is None:
            credentials = SystemCredentialStore()
        return cls(
            Path(settings.provider_store_path), credentials,
            builtin_ids=frozenset(PROVIDER_PRESETS),
        )

    # ── 持久化 ──

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            raise ProviderStoreError(f"供应商配置文件损坏: {error}") from error
        providers = data.get("providers") if isinstance(data, dict) else None
        if not isinstance(providers, list):
            raise ProviderStoreError("供应商配置文件形状非法（缺 providers 数组）")
        return providers

    def _save(self, providers: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "providers": providers}
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )

    # ── 查询 ──

    def list_entries(self) -> list[dict]:
        """全部自定义条目（含派生字段 kind / is_available / unavailable_reason）。"""
        return [self._with_derived(entry) for entry in self._load()]

    def get_entry(self, provider_id: str) -> dict:
        for entry in self._load():
            if entry.get("id") == provider_id:
                return self._with_derived(entry)
        raise KeyError(provider_id)

    def _with_derived(self, entry: dict) -> dict:
        """派生字段（不落盘）：kind（custom）+ is_available + unavailable_reason。

        is_available 是**已配置**语义（有凭据 ⇒ true），不是网络可达（ADR-0032
        D7）；凭据读取失败（后端不可用 / 被外部删除）按 credential_unavailable
        上报，不影响其他 provider（不变量 #21）。
        """
        derived = dict(entry)
        # 同 id 覆盖内置 preset ⇒ "override"；新 id ⇒ "custom"（§3.2 合并规则）。
        derived["kind"] = "override" if entry["id"] in self._builtin_ids else "custom"
        if self.credentials.get(entry["id"]) is not None:
            derived["is_available"] = True
            derived["unavailable_reason"] = None
        elif not self.credentials.available():
            derived["is_available"] = False
            derived["unavailable_reason"] = "credential_unavailable"
        else:
            derived["is_available"] = False
            derived["unavailable_reason"] = "missing_api_key"
        return derived

    def has_provider(self, provider_id: str) -> bool:
        return any(entry.get("id") == provider_id for entry in self._load())

    # ── 写路径 ──

    def create(self, body: dict) -> dict:
        """创建自定义供应商（id 冲突 = 覆盖更新，ADR-0032 §4）。

        带 `api_key` 的创建要么整体成功要么整体失败（凭据写失败 ⇒ 配置不落盘）。
        覆盖内置 preset（同 id）时 **models 取并集**（按 model_id 去重，自定义
        优先，§3.2）——"把 deepseek 指向自建代理"仍保留内置默认模型。
        """
        provider_id = self._require_id(body)
        entry = self._normalize(body, provider_id)
        if provider_id in self._builtin_ids:
            # 覆盖内置：models 并集（内置 preset 默认模型 + 自定义模型），去重
            # 且自定义优先（§3.2 合并规则）。
            from agent_harness.model.config import PROVIDER_PRESETS

            builtin_model = PROVIDER_PRESETS.get(provider_id, {}).get("model_name", "")
            builtin_rows = ([{"model_id": builtin_model}] if builtin_model else [])
            custom_ids = {m["model_id"] for m in entry["models"]}
            entry["models"] = [*entry["models"],
                               *(m for m in builtin_rows if m["model_id"] not in custom_ids)]
        api_key = body.get("api_key")
        if isinstance(api_key, str) and api_key:
            self._require_credential_backend()
            self.credentials.set(provider_id, api_key)
        providers = [e for e in self._load() if e.get("id") != provider_id]
        providers.append(entry)
        self._save(providers)
        return self._with_derived(entry)

    def update(self, provider_id: str, patch: dict) -> dict:
        """更新（`api_key` 省略 = 不改密钥；空串 = 显式清除，§7.1）。

        更新 base_url/models 时**不得**触碰已存在的凭据（§7.1 第 4 条）。
        """
        providers = self._load()
        for index, entry in enumerate(providers):
            if entry.get("id") != provider_id:
                continue
            merged = {**entry, **self._sanitize_patch(patch)}
            self._require_id(merged)
            if not validate_base_url(merged["base_url"]):
                raise ProviderStoreError(f"base_url 非法: {merged['base_url']!r}")
            api_key = patch.get("api_key")
            if isinstance(api_key, str) and api_key == "":
                # 显式清除（独立动作，不是"顺手清"）。
                self.credentials.delete(provider_id)
            elif isinstance(api_key, str) and api_key:
                self._require_credential_backend()
                self.credentials.set(provider_id, api_key)
            merged["updated_at"] = utc_now_iso()
            providers[index] = merged
            self._save(providers)
            return self._with_derived(merged)
        raise KeyError(provider_id)

    def delete(self, provider_id: str) -> None:
        """删除：**先删凭据**（失败 ⇒ 中止，配置保持原样）、再删配置（D6）。"""
        if not self.has_provider(provider_id):
            raise KeyError(provider_id)
        # 先删凭据：失败让"孤立可用密钥"的窗口保持为零。
        self.credentials.delete(provider_id)
        providers = [e for e in self._load() if e.get("id") != provider_id]
        self._save(providers)
        logger.info("供应商已删除（provider=%s）", provider_id)

    # ── 会话模型解析（D8：解析入口唯一） ──

    def resolve_provider_config(self, provider_id: str) -> dict:
        """自定义 provider 的生效配置（base_url / models / label）。

        覆盖内置 preset（同 id）时 base_url 以本条目为准（§3.2）。凭据由调用方
        经 `credentials.get()` 单独取——本方法**绝不**返回密钥。
        """
        return self.get_entry(provider_id)

    # ── 内部 ──

    def _require_id(self, body: dict) -> str:
        provider_id = body.get("id")
        if not isinstance(provider_id, str) or not validate_provider_id(provider_id):
            raise ProviderStoreError(f"provider id 非法（slug 规则）: {provider_id!r}")
        return provider_id

    def _require_credential_backend(self) -> None:
        if not self.credentials.available():
            raise CredentialError("当前平台没有可用的凭据存储，无法保存 API Key")

    def _sanitize_patch(self, patch: dict) -> dict:
        """只接受已知非密字段（api_key 由 update 单独处理，绝不进配置文件）。"""
        allowed = {"label", "base_url", "models", "last_test"}
        return {k: v for k, v in patch.items() if k in allowed}

    def _normalize(self, body: dict, provider_id: str) -> dict:
        base_url = body.get("base_url")
        if not isinstance(base_url, str) or not validate_base_url(base_url):
            raise ProviderStoreError(f"base_url 非法: {base_url!r}")
        models = body.get("models")
        if not isinstance(models, list) or not models:
            raise ProviderStoreError("models 必须是非空数组")
        normalized_models = []
        for model in models:
            if not isinstance(model, dict) or not str(model.get("model_id", "")).strip():
                raise ProviderStoreError("models 每条必须有非空 model_id")
            normalized_models.append({"model_id": model["model_id"],
                                      **({"label": model["label"]} if model.get("label") else {})})
        now = utc_now_iso()
        return {
            "id": provider_id,
            "label": body.get("label") or "",
            "base_url": base_url,
            "models": normalized_models,
            "created_at": now,
            "updated_at": now,
            **({"last_test": body["last_test"]} if body.get("last_test") else {}),
        }


def resolve_provider_target(
    store: ProviderStore, *, settings_provider: str, model_id: str,
) -> dict | None:
    """自定义供应商的会话模型解析（ADR-0032 D8/D9）。

    未命中（无该 provider 或该模型）返回 None——调用方给明确错误（含 provider
    id），**不静默 fallback** 到默认链（D9：删除后引用它的会话必须响亮失败）。
    返回 dict 不含密钥：凭据由调用方经 `store.credentials.get()` 单独取。
    """
    try:
        entry = store.get_entry(settings_provider)
    except KeyError:
        return None
    for model in entry.get("models", []):
        if model.get("model_id") == model_id:
            return {
                "provider": settings_provider,
                "model_id": model_id,
                "base_url": entry["base_url"],
                "label": entry.get("label") or "",
            }
    return None


def utc_now_iso() -> str:
    """当前 UTC 时刻（ISO 8601）。

    provider 配置的 `created_at` / `updated_at` 与连接测试的 `last_test.at`
    共用这一个——两处各写一份 `datetime.now(UTC).isoformat()` 就是给"同一字段
    两种格式"留门（#203 首版 model_providers 里确有一份私有副本）。
    """
    return datetime.now(UTC).isoformat()
