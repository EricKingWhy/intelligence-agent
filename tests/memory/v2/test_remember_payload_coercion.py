"""#1025：`remember_this` 的 `payload` 收到 stringified JSON 字符串时的 coercion 契约。

背景（#1025 实测）：经 OpenAI 兼容网关的模型（mimo）把嵌套参数 `payload`
双重编码成 JSON 字符串，`_RememberV2Args` 的判别联合（`extra="forbid"`）
无条件拒绝 → 工具校验失败、模型无法自修正（重试同型调用 10 次全败）。

修复口径（方案 A，用户 2026-10-12 裁决批准）：**字段作者在 args_schema
显式声明**接受字符串形态——`field_validator("payload", mode="before")`
对合法 JSON 字符串先 `json.loads` 再走正常判联合校验；解析失败原样返回
（保留 Pydantic 原错误，不静默吞错）；dict 入站零影响；不碰其他字段；
Executor 解析逻辑一行不改（spec 04 §5 主语是 Executor，args_schema 是
Tool Contract 的法定组成部分）。

消融对照：本文件先于实现提交时运行 = coercion「关」态（字符串被拒）；
实现后 =「开」态（同一 payload 通过）。数字进 #1025 回报。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import jwt
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from agent_harness.config import Settings
from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.memory.types import memory_session_var
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.index import InMemoryMemoryV2Index, MemoryV2IndexRelay
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.tools import RememberMemoryV2Tool, _RememberV2Args
from agent_harness.memory.v2.types import SemanticCategory, TrustedMemoryIdentity
from agent_harness.session import (
    RUN_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    SessionEvent,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from agent_harness.tooling.result import ErrorCode
from agent_harness.web.app import create_app

IDENTITY = IdentityContext("tenant-a", "user-a", ["user"])
TRUSTED = TrustedMemoryIdentity("tenant-a", "user-a")

# 票面同形状：模型把 payload 整个转义成 JSON 字符串（双重编码）。
STRING_PAYLOAD = json.dumps(
    {"kind": "semantic", "subject": "我", "fact": "喜欢喝美式", "category": "preference"},
    ensure_ascii=False,
)
STRING_ARGS = {
    "content": "我喜欢喝美式",
    "kind": "semantic",
    "payload": STRING_PAYLOAD,
}
DICT_ARGS = {
    "content": "我喜欢喝美式",
    "kind": "semantic",
    "payload": {
        "kind": "semantic", "subject": "我", "fact": "喜欢喝美式", "category": "preference",
    },
}


# ---------------------------------------------------------------------------
# schema 层：_RememberV2Args 的字段级 coercion 契约
# ---------------------------------------------------------------------------


class TestStringifiedPayloadCoercion:
    def test_valid_json_string_payload_passes_validation(self):
        """AC1 前半：合法 JSON 字符串 payload 通过校验，结果与 dict 入站等价。"""
        from_string = _RememberV2Args.model_validate(STRING_ARGS)
        from_dict = _RememberV2Args.model_validate(DICT_ARGS)
        assert from_string == from_dict
        assert from_string.payload.fact == "喜欢喝美式"

    def test_invalid_json_string_keeps_original_error(self):
        """消融② 阴性对照：非法 JSON 字符串仍被拒，保留 Pydantic 原错误，不静默吞错。"""
        with pytest.raises(ValidationError) as exc_info:
            _RememberV2Args.model_validate(
                {"content": "我喜欢喝美式", "kind": "semantic", "payload": "{not json"}
            )
        messages = "; ".join(err["msg"] for err in exc_info.value.errors())
        assert "Input should be a valid dictionary or object" in messages

    def test_dict_payload_unchanged(self):
        """AC1 后半回归：dict 入站行为逐字不变。"""
        args = _RememberV2Args.model_validate(DICT_ARGS)
        assert args.payload.subject == "我"
        assert args.payload.category is SemanticCategory.PREFERENCE

    @pytest.mark.parametrize("raw", ["123", "[1]", "\"只是一段字符串\""])
    def test_non_dict_parse_results_rejected(self, raw):
        """解析结果非 dict（int/list/str）→ 判联合正常拒绝、错误可读。"""
        with pytest.raises(ValidationError) as exc_info:
            _RememberV2Args.model_validate(
                {"content": "我喜欢喝美式", "kind": "semantic", "payload": raw}
            )
        messages = "; ".join(err["msg"] for err in exc_info.value.errors())
        assert "dictionary or object" in messages

    def test_parsed_dict_with_invalid_kind_rejected(self):
        """解析为 dict 但 kind 非法 → 判联合 kind 错误照常回模型（可自修正类）。"""
        bad = json.dumps(
            {"kind": "nonexistent", "subject": "我", "fact": "喜欢喝美式"},
            ensure_ascii=False,
        )
        with pytest.raises(ValidationError) as exc_info:
            _RememberV2Args.model_validate(
                {"content": "我喜欢喝美式", "kind": "semantic", "payload": bad}
            )
        messages = "; ".join(err["msg"] for err in exc_info.value.errors())
        assert "kind" in messages or "nonexistent" in messages

    def test_deep_nesting_does_not_bubble_recursionerror(self):
        """极端深嵌套字符串 → 不冒 RecursionError，收敛为 ValidationError。"""
        deep = "[" * 80_000 + "]" * 80_000
        with pytest.raises(ValidationError):
            _RememberV2Args.model_validate(
                {"content": "我喜欢喝美式", "kind": "semantic", "payload": deep}
            )

    def test_validator_chain_not_bypassed(self):
        """coercion 不制造校验漏洞：kind-tier 组合校验对字符串入站同样生效。

        #1440 F-05 推翻披露：原用例的前半段断言 payload.fact 必须是 content 的逐字摘录
        （`_payload_text_is_covered_by_content`）。#1353 §0 裁决 3 明定垫片放弃 verbatim
        校验、折中为 add 软校验（执行期比对本轮 user message），所以该硬闸已移除；
        参数面剩下的组合规则（kind ↔ payload 判别、profile ⇒ semantic）继续对字符串
        入站生效——后半段原样保留，前半段改为断言"照收"，软校验另有用例覆盖。
        """
        # 转述式 payload 不再被参数面拒绝（逐字校验已按裁决 3 移除）
        paraphrased = json.dumps(
            {"kind": "semantic", "subject": "我", "fact": "完全无关的内容",
             "category": "preference"},
            ensure_ascii=False,
        )
        args = _RememberV2Args.model_validate(
            {"content": "我喜欢喝美式", "kind": "semantic", "payload": paraphrased}
        )
        assert args.payload.fact == "完全无关的内容"

        # kind=procedural 配 semantic payload → _kind_tier_payload_combination 照拒
        semantic_for_procedural = json.dumps(
            {"kind": "semantic", "subject": "揉面", "fact": "直到面团均匀",
             "category": "preference"},
            ensure_ascii=False,
        )
        with pytest.raises(ValidationError):
            _RememberV2Args.model_validate(
                {"content": "用不锈钢盆揉面，直到面团均匀", "kind": "procedural",
                 "payload": semantic_for_procedural}
            )

    def test_schema_export_declares_object_forms_only(self):
        """§5.3.11：args_schema 导出的 JSON Schema 不因 before-validator 而引入 string 分支
        （schema 仍只声明对象形态——canonical 目标形态；str 容忍是运行时层，不误导模型面）。

        断言按结构而非子串匹配（不依赖 pydantic 序列化空格格式）：沿 payload 节点的
        联合分支逐个解引用 `$defs` 内的定义，每个分支的顶层类型都必须是 object——
        任何分支声明 type=string（含 type 列表变体）即失败。分支内的叶子字段
        （subject/fact 等）合法声明 string，不在此判据内。

        #1440 F-05：`payload` 现在可缺省（运行时由 content 合成），联合里因此多一个
        `type: null` 分支——断言改为"对象形态恰好 3 个、其余只允许 null"，仍禁止 string。
        """
        schema = _RememberV2Args.model_json_schema()
        payload_node = schema["properties"]["payload"]

        def _iter_branches(node: dict) -> Iterator[dict]:
            """产出 payload 声明里的每个联合分支（把 `$ref` 解引用到 `$defs`）。"""
            if "$ref" in node:
                ref = node["$ref"]
                assert ref.startswith("#/$defs/"), ref
                yield schema["$defs"][ref.rsplit("/", 1)[-1]]
                return
            sub = [b for key in ("oneOf", "anyOf", "allOf") for b in node.get(key, ())]
            if sub:
                for branch in sub:
                    yield from _iter_branches(branch)
            else:
                yield node

        branches = list(_iter_branches(payload_node))
        objects = [b for b in branches if b.get("type") == "object"]
        assert len(objects) == 3  # semantic / episodic / procedural 三个对象形态
        assert all(b.get("type") in ("object", "null") for b in branches), branches


# ---------------------------------------------------------------------------
# 全链路：字符串 payload → 校验 → service.create → 落库（AC1 / AC2 证据）
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def memory_service(tmp_path):
    """service + store 成对返回（index 不被任何用例直接使用，不再返回第三元）。"""
    store = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
    await store.initialize()
    index = InMemoryMemoryV2Index()
    relay = MemoryV2IndexRelay(store, index)
    return MemoryV2Service(store, index, relay=relay), store


async def _write_user_turn(sessions: JsonlSessionStore, session_id: str, text: str) -> str:
    user_event = SessionEvent(
        seq=0, type=USER_MESSAGE, session_id=session_id, data={"content": text},
    )
    start_event = SessionEvent(
        seq=1, type=RUN_STARTED, session_id=session_id, run_id="run-1", data={},
    )
    sessions.append_event(session_id, user_event)
    sessions.append_event(session_id, start_event)
    return user_event.event_id


class _ConsentHarness:
    """remember_this 需要当前用户轮 consent（governance 契约），按用例装配会话上下文。"""

    def __init__(self, tmp_path) -> None:
        self.sessions = JsonlSessionStore(root=tmp_path / "sessions")
        self.session_id = "coercion-session"

    async def open(self, text: str = "记住我喜欢喝美式") -> str:
        return await _write_user_turn(self.sessions, self.session_id, text)

    def bind(self):
        return memory_session_var.set(self.session_id)


@contextmanager
def _identity_bound(harness: _ConsentHarness):
    """identity + memory session 绑定的 try-finally 样板收敛（4 个 async 用例共用）：
    进入时设 identity 上下文与 session 绑定，退出时按入栈逆序复位。"""
    identity_token = set_identity_context(IDENTITY)
    binding = harness.bind()
    try:
        yield
    finally:
        memory_session_var.reset(binding)
        identity_context_var.reset(identity_token)


@pytest.mark.asyncio
async def test_string_payload_record_lands_in_store(memory_service, tmp_path):
    """AC1 全链路：字符串 payload 经 tool.execute 落库（不再恒 INVALID_ARGUMENT）。"""
    service, store = memory_service
    harness = _ConsentHarness(tmp_path)
    event_id = await harness.open()
    tool = RememberMemoryV2Tool(service, harness.sessions)
    with _identity_bound(harness):
        result = await tool.execute(_RememberV2Args.model_validate(STRING_ARGS))

    assert result.ok, result.message
    record = await store.get(result.data["memory_id"], TRUSTED)
    assert record.payload.fact == "喜欢喝美式"
    assert record.source_event_ids == [event_id]


@pytest.mark.asyncio
async def test_dict_payload_record_lands_in_store(memory_service, tmp_path):
    """AC1 后半全链路回归：dict payload 落库路径不变。"""
    service, store = memory_service
    harness = _ConsentHarness(tmp_path)
    await harness.open()
    tool = RememberMemoryV2Tool(service, harness.sessions)
    with _identity_bound(harness):
        result = await tool.execute(_RememberV2Args.model_validate(DICT_ARGS))

    assert result.ok, result.message
    record = await store.get(result.data["memory_id"], TRUSTED)
    assert record.payload.fact == "喜欢喝美式"


# ---------------------------------------------------------------------------
# executor 文案（#1025 修法②）：stringified 命中时不再邀请注定失败的重试
# ---------------------------------------------------------------------------


class _ObjPayload(BaseModel):
    kind: str
    text: str


class _ObjArgs(BaseModel):
    payload: _ObjPayload


class _ObjectFieldTool(Tool):
    def __init__(self) -> None:
        self.call_count = 0

    @property
    def name(self) -> str:
        return "object_field_tool"

    @property
    def description(self) -> str:
        return "接收一个对象参数 payload。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _ObjArgs

    async def execute(self, args: BaseModel) -> ToolResult:
        self.call_count += 1
        return ToolResult.success("ok")


def _make_executor() -> tuple[ToolExecutor, _ObjectFieldTool]:
    reg = ToolRegistry()
    obj_tool = _ObjectFieldTool()
    reg.register(obj_tool)
    return ToolExecutor(reg), obj_tool


class TestExecutorStringifiedMessage:
    @pytest.mark.asyncio
    async def test_stringified_hit_message_is_deterministic(self):
        """stringified 命中：文案改为确定性表述（双重编码 / 重试不会成功 / 以对象形态重发），
        不再说「请修正参数后重新调用」；retryable 保持 False；execute 次数仍为 0。"""
        executor, obj_tool = _make_executor()
        tc = {
            "id": "call-1025-a",
            "name": "object_field_tool",
            "args": {"payload": json.dumps({"kind": "semantic", "text": "美式"})},
        }

        execution = await executor.execute(tc)

        assert execution.result.ok is False
        assert execution.result.error_code == ErrorCode.INVALID_ARGUMENT
        assert execution.result.retryable is False
        message = execution.result.message
        assert "双重编码" in message
        assert "重试不会成功" in message or "不会成功" in message
        assert "直接传对象" in message  # 与 test_executor_767 的既有断言兼容
        assert "请修正参数后重新调用" not in message
        assert obj_tool.call_count == 0

    @pytest.mark.asyncio
    async def test_non_stringified_message_unchanged(self):
        """非 stringified 的普通参数错误：原文案不变（仍含「请修正参数后重新调用」）。"""
        executor, _ = _make_executor()
        tc = {
            "id": "call-1025-b",
            "name": "object_field_tool",
            "args": {"payload": "这不是 JSON，只是一段普通文本"},
        }

        execution = await executor.execute(tc)

        assert execution.result.ok is False
        assert "请修正参数后重新调用" in execution.result.message
        assert "JSON 字符串" not in execution.result.message


@pytest.mark.asyncio
async def test_stringified_remember_call_succeeds_on_first_attempt(memory_service, tmp_path):
    """AC2 证据：同型场景（双重编码 payload）经 executor 首次调用即成功收敛，
    不再产生同 fingerprint 的重复失败流。常数依据：agent/guards.py 的
    STUCK_PATTERN_TOOL_FAILURE=3（soft 守卫阈值）——同类失败达 3 即熔断，
    本修复让该场景 1 次即收敛，远低于守卫线。"""
    service, store = memory_service
    harness = _ConsentHarness(tmp_path)
    await harness.open()
    tool = RememberMemoryV2Tool(service, harness.sessions)
    reg = ToolRegistry()
    reg.register(tool)
    executor = ToolExecutor(reg)
    with _identity_bound(harness):
        execution = await executor.execute({
            "id": "call-1025-c",
            "name": "remember_this",
            "args": dict(STRING_ARGS),
        })

    assert execution.result.ok, execution.result.message
    assert await store.get(execution.result.data["memory_id"], TRUSTED)


# ---------------------------------------------------------------------------
# AC3 等价集成证据（checkpoint §5.5：真机 MiMo E2E 条件不具备时的替代路径）
# OpenAI 兼容网关形状：工具调用 args 含字符串 payload（双重编码）→ executor →
# 落库（真实 SQLite + 真实 service）→ GET /api/memories 返回该条。
# 全链路无 mock 冒充：网关层的字符串化用测试侧构造入参复现，其余全是真组件。
# ---------------------------------------------------------------------------

_JWT_SECRET = "memory-payload-coercion-signing-secret-at-least-32"


def _bearer(user_id: str) -> dict[str, str]:
    encoded = jwt.encode({
        "tenant_id": "tenant-a", "user_id": user_id,
        "scopes": ["user"],
        "exp": int(datetime.now(UTC).timestamp()) + 600,
    }, _JWT_SECRET)
    return {"Authorization": f"Bearer {encoded}"}


@pytest.mark.asyncio
async def test_string_payload_visible_through_web_memory_api(tmp_path, monkeypatch):
    # web 层走与 memory_app fixture 相同的真实装配（真 SQLite service）。
    # 记忆写入与 GET 用的是**同一个** app 装配出的 service ⇒ 全链路无 mock 冒充。
    monkeypatch.setattr(MemoryV2Service, "start_tombstone_purger", lambda _self: None)
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        jwt_secret=_JWT_SECRET, capabilities='{"memory": {"provider": "langmem"}}',
    )
    app = create_app(settings, enable_cors=False,
    allow_testserver=True,
    )
    with TestClient(app) as client:
        _registry, wiring = await client.app.state.agent.get_wiring()
        service = wiring.memory_v2
        assert service is not None

        harness = _ConsentHarness(tmp_path)
        await harness.open()
        tool = RememberMemoryV2Tool(service, harness.sessions)
        reg = ToolRegistry()
        reg.register(tool)
        executor = ToolExecutor(reg)
        with _identity_bound(harness):
            execution = await executor.execute({
                "id": "call-1025-d",
                "name": "remember_this",
                "args": dict(STRING_ARGS),
            })
        assert execution.result.ok, execution.result.message

        resp = client.get("/api/memories", headers=_bearer("user-a"))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["content"] for item in body] == ["我喜欢喝美式"]
