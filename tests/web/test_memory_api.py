"""用户侧记忆 API（#159 B）：列出 / 硬删 / 错误语义 / 归属隔离 / 审计不落 SessionEvent。

装配方式：真 `create_app` + 真 `wire_capabilities`，只把 memory 的**组件工厂**换成 fake
（`FakeMemoryCapability` 的 namespace/归属语义是真的，被替换的只是"要不要连 Zilliz"）。
身份走真 JWT 中间件——AC7 要求的"越权在领域层也被拒"必须在这种端到端形状下才算验过。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from agent_harness.capability.base import DegradeReason
from agent_harness.config import Settings
from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.memory.fake_capability import FakeMemoryCapability
from agent_harness.memory.types import MemoryScope, memory_session_var
from agent_harness.memory.v2.tools import _RememberV2Args
from agent_harness.memory.v2.types import (
    MemoryKind,
    SemanticCategory,
    SemanticPayload,
    TrustedMemoryIdentity,
)
from agent_harness.memory.v2.types import (
    MemoryScope as MemoryScopeV2,
)
from agent_harness.memory.vector_store import VectorStoreError
from agent_harness.session import Session
from agent_harness.session.event import (
    EVENT_TYPES as SESSION_EVENT_TYPES,
)
from agent_harness.session.event import (
    MEMORY_RECALLED,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.web.app import create_app
from agent_harness.web.memory import _DEGRADED_MESSAGE
from tests.memory.v2._records import make_draft

_SECRET = "memory-api-test-signing-secret-at-least-32"
_ALICE = IdentityContext("acme", "alice", ["user"])
_BOB = IdentityContext("acme", "bob", ["user"])
#: 认证通过但**没有** "user" scope：`MemoryNamespace.of` 的 scope 授权会拒绝。
_NO_USER_SCOPE = IdentityContext("acme", "carol", ["session"])
#: 带 "session" scope 的 alice：HTTP 请求没有可信 session 绑定（`memory_session_var`
#: 只在 detached run 里设置），所以按 id 去删会话记忆时解析不出该行的 namespace。
_SESSION_ALICE = IdentityContext("acme", "alice", ["user", "session"])


class _FakeMemoryComponents:
    """provider seam 的最小合法产物：capability + writeback + 生命周期。"""

    def __init__(self) -> None:
        self.capability = FakeMemoryCapability()
        self.writeback = object()
        self.initialized = False
        self.closed = False

    async def initialize(self) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed = True


def _token(identity: IdentityContext) -> str:
    return jwt.encode(
        {
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "scopes": list(identity.scopes),
            "exp": int(datetime.now(UTC).timestamp()) + 600,
        },
        _SECRET,
    )


def _auth(identity: IdentityContext) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(identity)}"}


@pytest.fixture
def memory_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真 app + fake memory provider；返回 (client, components)。"""
    components = _FakeMemoryComponents()
    monkeypatch.setattr(
        "agent_harness.capability.factories.build_memory_components",
        lambda settings, *, provider="builtin": components,
    )
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        jwt_secret=_SECRET,
        capabilities='{"memory": {"provider": "langmem"}}',
    )
    app = create_app(settings, enable_cors=False)
    with TestClient(app) as client:
        yield client, components


async def _seed(components: _FakeMemoryComponents, identity: IdentityContext,
                content: str, metadata: dict | None = None, *,
                scope: MemoryScope = MemoryScope.USER, session_id: str | None = None) -> str:
    """在指定身份下直接经能力写一条记忆（等价于后台抽取写进来的那条）。

    `scope=SESSION` 时必须给 `session_id`：namespace 解析要求可信的会话绑定。
    """
    token = set_identity_context(identity)
    binding = memory_session_var.set(session_id) if session_id is not None else None
    try:
        return await components.capability.store(scope, content, metadata or {})
    finally:
        if binding is not None:
            memory_session_var.reset(binding)
        identity_context_var.reset(token)


async def _contents(components: _FakeMemoryComponents, identity: IdentityContext) -> list[str]:
    token = set_identity_context(identity)
    try:
        return [entry.content for entry in await components.capability.list_entries(MemoryScope.USER, 50)]
    finally:
        identity_context_var.reset(token)


async def _seed_v2(client: TestClient, identity: IdentityContext, content: str):
    """Seed the authoritative V2 store without relying on the model or HTTP write path."""
    _registry, wiring = await client.app.state.agent.get_wiring()
    assert wiring.memory_v2 is not None
    return await wiring.memory_v2.create(
        make_draft(
            content=content,
            source_event_ids=[hashlib.sha256(content.encode("utf-8")).hexdigest()],
        ),
        TrustedMemoryIdentity(identity.tenant_id, identity.user_id),
    )


@pytest.mark.asyncio
async def test_list_returns_only_the_callers_memories(memory_app):
    """AC5 + AC7：列出的永远是**自己** namespace 的记忆（身份来自可信入口，不是参数）。"""
    client, components = memory_app
    await _seed(components, _ALICE, "alice 喜欢 TypeScript")
    await _seed(components, _BOB, "bob 的秘密")

    resp = client.get("/api/memories", headers=_auth(_ALICE))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [item["content"] for item in body] == ["alice 喜欢 TypeScript"]
    assert body[0]["scope"] == "user"


@pytest.mark.asyncio
async def test_list_strips_provider_internal_metadata(memory_app):
    """列表给用户看的是自己的 metadata，不含 provider 内部载荷。"""
    client, components = memory_app
    await _seed(components, _ALICE, "alice 的偏好",
                {"importance": 0.8, "_langmem_value": {"kind": "MemoryPayload", "raw": "内部"}})

    [item] = client.get("/api/memories", headers=_auth(_ALICE)).json()

    assert item["metadata"] == {"importance": 0.8}


@pytest.mark.asyncio
async def test_list_paginates_and_clamps(memory_app):
    """分页：offset/limit 切片；越界的 limit 由 FastAPI 422 挡在领域层之前。"""
    client, components = memory_app
    for index in range(3):
        await _seed(components, _ALICE, f"第 {index} 条")

    page = client.get("/api/memories?limit=2&offset=1", headers=_auth(_ALICE)).json()
    full = client.get("/api/memories?limit=50", headers=_auth(_ALICE)).json()
    assert {item["content"] for item in full} == {"第 0 条", "第 1 条", "第 2 条"}
    # 分页 = **同一个倒序口径**的连续切片（写成切片比较而不是硬编码顺序：同一微秒内的
    # 排序 tie-break 是随机 uuid，靠它推出来的固定顺序会变成偶发 flake）。
    assert page == full[1:3]
    # 倒序契约本身：created_at 单调不增。
    stamps = [item["created_at"] for item in full]
    assert stamps == sorted(stamps, reverse=True)

    assert client.get("/api/memories?limit=0", headers=_auth(_ALICE)).status_code == 422
    assert client.get("/api/memories?limit=10000", headers=_auth(_ALICE)).status_code == 422
    assert client.get("/api/memories?offset=-1", headers=_auth(_ALICE)).status_code == 422
    assert client.get("/api/memories?offset=10001", headers=_auth(_ALICE)).status_code == 422
    assert client.get(
        f"/api/memories?offset={2**63}", headers=_auth(_ALICE),
    ).status_code == 422


@pytest.mark.asyncio
async def test_delete_forgets_then_reports_404(memory_app):
    """AC5/AC6：删掉 → 200；再删同一条 → **404**（"这条已经不在了"要报出来，不是静默 204）。"""
    client, components = memory_app
    memory_id = await _seed(components, _ALICE, "要被遗忘的偏好")

    first = client.delete(f"/api/memories/{memory_id}", headers=_auth(_ALICE))
    assert first.status_code == 200, first.text
    assert first.json() == {"id": memory_id, "deleted": True}
    assert await _contents(components, _ALICE) == []

    second = client.delete(f"/api/memories/{memory_id}", headers=_auth(_ALICE))
    assert second.status_code == 404, second.text
    assert memory_id in second.json()["detail"]


@pytest.mark.asyncio
async def test_unknown_id_is_404(memory_app):
    client, _ = memory_app
    resp = client.delete("/api/memories/never-existed", headers=_auth(_ALICE))
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_deleting_someone_elses_memory_is_403_and_changes_nothing(memory_app):
    """AC7：越权由**领域层**拒绝（不只是查询条件），且对方数据完好。

    这里 alice 直接拿着 bob 的 memory_id 来删——最坏形状。403（而不是伪装成 404），
    因为领域层的归属校验本来就拒绝了，如实上报。
    """
    client, components = memory_app
    bob_memory = await _seed(components, _BOB, "bob 的秘密")

    resp = client.delete(f"/api/memories/{bob_memory}", headers=_auth(_ALICE))

    assert resp.status_code == 403, resp.text
    assert await _contents(components, _BOB) == ["bob 的秘密"]
    assert await _contents(components, _ALICE) == []


@pytest.mark.asyncio
async def test_list_without_user_scope_is_403_not_500(memory_app):
    """AC6：身份认证通过但缺 "user" scope → **403**，不是 500。

    scope 授权由 `MemoryNamespace.of` 拒绝；不翻译就会以未登记领域异常的形状冒成 500，
    正是 AC6 要挡的。归属校验不依赖 "user" scope，所以同一个身份拿别人的 id 去删也是 403
    ——两个入口给出同一类明确状态码，且对方的记忆完好。
    """
    client, components = memory_app
    alice_memory = await _seed(components, _ALICE, "alice 的偏好")

    listing = client.get("/api/memories", headers=_auth(_NO_USER_SCOPE))
    assert listing.status_code == 403, listing.text

    deleting = client.delete(f"/api/memories/{alice_memory}", headers=_auth(_NO_USER_SCOPE))
    assert deleting.status_code == 403, deleting.text
    assert await _contents(components, _ALICE) == ["alice 的偏好"]


@pytest.mark.asyncio
async def test_deleting_a_session_scoped_memory_is_403_not_500(memory_app):
    """AC6：HTTP 入口只暴露 USER scope，会话记忆不能用 500 冒出去，也不能被它删掉。

    alice 的 token 带 "session" scope，但 HTTP 请求没有可信的会话绑定，所以按 id 解析那一行的
    namespace 会失败。领域层的归属比较把"这个上下文解析不出这一行"判定为"不是你能动的" →
    403（跨用户那条也是 403，语义一致）；记忆完好由随后的带绑定读取证明。
    """
    client, components = memory_app
    memory_id = await _seed(components, _SESSION_ALICE, "会话内的临时偏好",
                           scope=MemoryScope.SESSION, session_id="sess-1")

    resp = client.delete(f"/api/memories/{memory_id}", headers=_auth(_SESSION_ALICE))

    assert resp.status_code == 403, resp.text
    token = set_identity_context(_SESSION_ALICE)
    binding = memory_session_var.set("sess-1")
    try:
        remaining = await components.capability.list_entries(MemoryScope.SESSION, 10)
    finally:
        memory_session_var.reset(binding)
        identity_context_var.reset(token)
    assert [entry.content for entry in remaining] == ["会话内的临时偏好"]


@pytest.mark.asyncio
async def test_memory_capability_disabled_is_503(tmp_path: Path):
    """没配 memory 能力时端点无从服务：503（配置状态，不是"资源不存在"）。"""
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test", jwt_secret=_SECRET,
    )
    with TestClient(create_app(settings, enable_cors=False)) as client:
        list_resp = client.get("/api/memories", headers=_auth(_ALICE))
        assert list_resp.status_code == 503
        assert client.delete("/api/memories/x", headers=_auth(_ALICE)).status_code == 503

    # #225：503 的 detail 是 `{code, message}`——code 是给前端的机读判别字段
    # （不靠匹配中文），message 是给人看的那句话。
    detail = list_resp.json()["detail"]
    assert detail["code"] == DegradeReason.NOT_CONFIGURED.value
    assert "CAPABILITIES" in detail["message"]


@pytest.mark.asyncio
async def test_memory_init_failure_503_says_fault_not_config_state(tmp_path: Path, monkeypatch):
    """配了 memory 但装配期抛异常（向量库不可达）→ 503 必须说"故障"，且不给"去配 CAPABILITIES"的错线索。

    真机症状（#225）：`CAPABILITIES` 里配着 memory，工厂里向量库连不上，装配期按
    OPTIONAL_RUNTIME 降级；路由层把"没配"与"配了但坏了"塌成同一句话，用户于是去改一个
    本来就配好的开关，怎么改都没用。这条与上一条配对，锁"两条 503 的码与文案都不同"。
    """

    def _unreachable(settings, *, provider="builtin"):
        raise VectorStoreError("Memory vector store: unavailable")

    monkeypatch.setattr(
        "agent_harness.capability.factories.build_memory_components", _unreachable,
    )
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        jwt_secret=_SECRET,
        capabilities='{"memory": {"provider": "langmem"}}',
    )
    with TestClient(create_app(settings, enable_cors=False)) as client:
        resp = client.get("/api/memories", headers=_auth(_ALICE))

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["code"] == DegradeReason.INIT_FAILED.value
    assert "初始化失败" in detail["message"]
    # 判别性断言：那条把人指向本来就是开的开关的线索，在这个原因下**一个字都不许有**。
    assert "请在 CAPABILITIES 中配置 memory" not in detail["message"]


def test_every_degrade_reason_has_a_message():
    """逐原因的文案表必须**覆盖整个枚举**（`_DEGRADED_MESSAGE[reason]` 是直接索引）。

    给 `DegradeReason` 添第 5 个码而不加文案 ⇒ 那条路径 500（而不是 503）。
    枚举与表分居两个模块，没有这条钉子就只能靠人记得两边一起改。"""
    assert set(_DEGRADED_MESSAGE) == set(DegradeReason)


@pytest.mark.asyncio
async def test_audit_goes_to_structured_logs_not_session_events(memory_app, caplog):
    """AC8：审计落 memory 侧结构化日志，且**不**进会话事件流。

    两头都断言：日志里有一条 `memory_forget`（入口 api、结果 forgotten），而
    SessionEvent 的词汇表里**不存在**记忆变更事件（有人将来想加，会先撞红这里）。
    """
    client, components = memory_app
    memory_id = await _seed(components, _ALICE, "要被遗忘的偏好")

    with caplog.at_level(logging.INFO, logger="agent_harness.memory.audit"):
        assert client.delete(f"/api/memories/{memory_id}", headers=_auth(_ALICE)).status_code == 200

    [record] = [r for r in caplog.records if getattr(r, "event_type", None) == "memory_forget"]
    assert record.entry_point == "api"
    assert record.outcome == "forgotten"
    assert record.memory_id == memory_id
    assert record.tenant_id == "acme" and record.user_id == "alice"

    # 会话事件流里不得出现"记忆**内容**变更"类事件——将来有人想加"memory/forget"之类，
    # 会先撞红这里。**注意口径**：`memory/degraded` 是**降级观测**（抽取/写回/检索失败），
    # 不是内容变更，AC8 明说它不构成反例，所以这里不能写成"任何含 memory 的事件都不许有"。
    #
    # `memory/updated` 同理不构成反例（**2026-09-24 修**）：它由 PRD V2 §6.5 与
    # #298 R12 明确规定——"committed changes emit `memory/updated`"，载荷只有
    # count / memory IDs / action counts / job ID，**不带内容**（`MEMORY_UPDATED` 的
    # 写入点 `runner.SessionEventSink` 与 T6 的单事务提交同源）。此前它被下面的
    # `"memory/update"` 子串误伤：那条断言写于只有 `memory/degraded` 的年代，
    # T6（`2f5a32e`）把 `memory/updated` 登记进词表后这条就一直红着——本文件由
    # #298 T7b 的门禁发现。缺的正是当时那份例外名单里的第二项。
    forbidden = [t for t in SESSION_EVENT_TYPES if t != "memory/updated" and any(
        word in t.lower() for word in ("forget", "delet", "memory/update"))]
    assert forbidden == []


def test_origin_gate_is_wired_on_memory_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """本地信任模式下跨源请求被来源闸拒绝（与项目 API 共用同一条规则）。

    这条必须在**没有 jwt_secret** 的 app 上验：配置了 JWT 时认证层才是边界，来源闸直接
    让位（跨源网页拿不到签名 token）——那正是 projects 那条测试的结论。
    """
    components = _FakeMemoryComponents()
    monkeypatch.setattr(
        "agent_harness.capability.factories.build_memory_components",
        lambda settings, *, provider="builtin": components,
    )
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        capabilities='{"memory": {"provider": "langmem"}}',
    )
    with TestClient(create_app(settings, enable_cors=False)) as client:
        cross_origin = client.get("/api/memories", headers={"Origin": "http://evil.example"})
        assert cross_origin.status_code == 403, cross_origin.text
        # 非浏览器发起（无 Origin）放行——本地信任模式的既定口径。
        assert client.get("/api/memories").status_code == 200


@pytest.mark.asyncio
async def test_v2_list_filters_detail_versions_edit_stale_version_and_identity(memory_app):
    client, _components = memory_app
    record = await _seed_v2(client, _ALICE, "用户偏好简洁的解释")
    case_folded = await _seed_v2(client, _ALICE, "用户使用 TypeScript")
    headers = _auth(_ALICE)

    listed = client.get(
        "/api/memories?q=解释&kind=semantic&status=active&scope=user_global&limit=10&offset=0",
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [record.id]
    assert client.get(
        "/api/memories?q=typescript&kind=semantic", headers=headers,
    ).json()[0]["id"] == case_folded.id
    assert client.get("/api/memories?kind=episodic", headers=headers).json() == []
    assert client.get("/api/memories?scope=project", headers=headers).json() == []
    all_v2 = client.get("/api/memories?kind=semantic&limit=10", headers=headers).json()
    second_page = client.get(
        "/api/memories?kind=semantic&limit=1&offset=1", headers=headers,
    ).json()
    assert len(all_v2) == 2 and second_page == all_v2[1:2]
    assert client.get(
        "/api/memories?kind=semantic&limit=1&offset=2", headers=headers,
    ).json() == []

    detail = client.get(f"/api/memories/{record.id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["source_type"] == "automatic"
    assert detail.json()["evidence"] == [{
        "role": "user", "hash": "hash-1",
    }]
    assert "excerpt" not in detail.text
    assert len(client.get(f"/api/memories/{record.id}/versions", headers=headers).json()) == 1

    edit_body = {
        "expected_version": 1,
        "content": "用户编辑后的偏好",
        "payload": {
            "kind": "semantic", "subject": "回答风格", "fact": "用户编辑后的偏好",
            "category": "preference",
        },
    }
    edited = client.patch(f"/api/memories/{record.id}", headers=headers, json=edit_body)
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2 and edited.json()["source_type"] == "user_edit"
    edited_id = edited.json()["id"]
    mismatched = {
        **edit_body,
        "expected_version": 2,
        "payload": {
            "kind": "episodic", "situation": "situation", "action": "action",
            "outcome": "outcome", "lesson": "lesson",
        },
    }
    assert client.patch(
        f"/api/memories/{edited_id}", headers=headers, json=mismatched,
    ).status_code == 422
    blank = {**edit_body, "expected_version": 2, "content": "  "}
    assert client.patch(
        f"/api/memories/{edited_id}", headers=headers, json=blank,
    ).status_code == 422
    stale = client.patch(
        f"/api/memories/{edited_id}", headers=headers, json=edit_body,
    )
    assert stale.status_code == 409
    assert len(client.get(f"/api/memories/{record.id}/versions", headers=headers).json()) == 2

    assert client.get(f"/api/memories/{record.id}", headers=_auth(_BOB)).status_code == 404
    forged = {**edit_body, "user_id": "bob"}
    assert client.patch(
        f"/api/memories/{record.id}", headers=headers, json=forged,
    ).status_code == 422


@pytest.mark.asyncio
async def test_v2_project_filter_uses_resolved_workspace_and_identity(memory_app, tmp_path):
    client, _components = memory_app
    state = client.app.state.agent
    await state.ensure_stores()
    project_path = tmp_path / "authorized-project"
    project_path.mkdir()
    workspace = await state.workspace_index.create(project_path, title="Authorized project")
    _registry, wiring = await state.get_wiring()
    assert wiring.memory_v2 is not None
    trusted = TrustedMemoryIdentity(_ALICE.tenant_id, _ALICE.user_id, workspace.id)
    record = await wiring.memory_v2.create(make_draft(
        scope=MemoryScopeV2.PROJECT, project_id=workspace.id,
        content="project-only preference", source_event_ids=["project-api-event"],
    ), trusted)

    response = client.get(
        f"/api/memories?scope=project&project_id={workspace.id}", headers=_auth(_ALICE),
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [record.id]
    assert client.get(
        "/api/memories?scope=project&project_id=unknown-project", headers=_auth(_ALICE),
    ).status_code == 404


@pytest.mark.asyncio
async def test_web_remember_tool_uses_project_scope_from_workspace_ledger(memory_app, tmp_path):
    client, _components = memory_app
    state = client.app.state.agent
    await state.ensure_stores()
    project_path = tmp_path / "remember-project"
    project_path.mkdir()
    workspace = await state.workspace_index.create(project_path, title="Remember project")
    session_id = "remember-project-session"
    session = Session.start(state.store, session_id=session_id, cwd=project_path)
    session.append(USER_MESSAGE, {"content": "Please remember this project uses pnpm"})
    await state.workspace_index.attach_session(session_id)

    _registry, wiring = await state.get_wiring()
    remember = next(tool for tool in wiring.tools if tool.name == "remember_this")
    assert remember._workspace_index is state.workspace_index
    assert wiring.memory_v2 is not None

    identity_token = set_identity_context(_ALICE)
    session_token = memory_session_var.set(session_id)
    try:
        result = await remember.execute(_RememberV2Args(
            content="this project uses pnpm",
            kind=MemoryKind.SEMANTIC,
            payload=SemanticPayload(
                subject="project tooling", fact="uses pnpm",
                category=SemanticCategory.PROJECT_FACT,
            ),
        ))
    finally:
        memory_session_var.reset(session_token)
        identity_context_var.reset(identity_token)

    assert result.ok
    trusted = TrustedMemoryIdentity(_ALICE.tenant_id, _ALICE.user_id, workspace.id)
    record = await wiring.memory_v2._store.get(result.data["memory_id"], trusted)
    assert record.scope is MemoryScopeV2.PROJECT
    assert record.project_id == workspace.id


@pytest.mark.asyncio
async def test_v2_delete_is_idempotent_erases_index_and_leaves_only_tombstone(memory_app):
    from agent_harness.memory.v2._sqlite import connect
    from agent_harness.memory.v2.index import MemoryV2IndexRelay

    client, _components = memory_app
    record = await _seed_v2(client, _ALICE, "只在删除前存在的正文")
    _registry, wiring = await client.app.state.agent.get_wiring()
    service = wiring.memory_v2
    assert service is not None
    trusted = TrustedMemoryIdentity("acme", "alice")
    await MemoryV2IndexRelay(service._store, service._index).flush()
    assert await service.search(
        "删除前存在", trusted, scope=MemoryScopeV2.USER_GLOBAL, limit=10,
    )

    deleted = client.delete(f"/api/memories/{record.id}", headers=_auth(_ALICE))
    replay = client.delete(f"/api/memories/{record.id}", headers=_auth(_ALICE))
    assert deleted.status_code == 200 and deleted.json() == {"id": record.id, "deleted": True}
    assert replay.status_code == 200 and replay.json() == {"id": record.id, "deleted": False}
    assert await service.search(
        "删除前存在", trusted, scope=MemoryScopeV2.USER_GLOBAL, limit=10,
    ) == []

    async with connect(service._store.database_path) as connection:
        async with connection.execute("PRAGMA table_info(memory_v2_tombstones)") as cursor:
            columns = await cursor.fetchall()
        async with connection.execute("SELECT * FROM memory_v2_tombstones") as cursor:
            tombstones = await cursor.fetchall()
        async with connection.execute("SELECT content FROM memory_v2_records") as cursor:
            records = await cursor.fetchall()
    assert {row["name"] for row in columns} == {
        "memory_id", "root_id", "tenant_id", "user_id", "scope", "project_id",
        "deleted_at", "expires_at", "deletion_reason", "content_hashes", "source_hashes",
    }
    assert len(tombstones) == 1 and records == []
    assert "只在删除前存在的正文" not in str(dict(tombstones[0]))


@pytest.mark.asyncio
async def test_v2_index_failure_reports_committed_delete_and_retains_retry(memory_app):
    from agent_harness.memory.v2.index import MemoryV2IndexRelay

    client, _components = memory_app
    record = await _seed_v2(client, _ALICE, "索引故障时仍已删除")
    _registry, wiring = await client.app.state.agent.get_wiring()
    service = wiring.memory_v2
    assert service is not None
    trusted = TrustedMemoryIdentity("acme", "alice")
    await MemoryV2IndexRelay(service._store, service._index).flush()
    service._index.fail_delete = RuntimeError("injected index failure")

    response = client.delete(f"/api/memories/{record.id}", headers=_auth(_ALICE))
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "memory_index_delete_pending"
    with pytest.raises(KeyError):
        await service.read(record.id, trusted)
    assert [item.operation.value for item in await service._store.pending()] == ["delete"]
    assert await service.search(
        "索引故障", trusted, scope=MemoryScopeV2.USER_GLOBAL, limit=10,
    ) == []

    service._index.fail_delete = None
    await service._relay.flush()
    assert await service._store.pending() == []


@pytest.mark.asyncio
async def test_v2_bulk_confirmation_settings_and_session_recall_redaction(memory_app):
    client, _components = memory_app
    first = await _seed_v2(client, _ALICE, "用户偏好第一条")
    second = await _seed_v2(client, _ALICE, "用户偏好第二条")
    headers = _auth(_ALICE)

    assert client.post(
        "/api/memories/bulk-delete", headers=headers,
        json={"kind": "semantic", "confirmation": "delete"},
    ).status_code == 422
    settings1 = client.patch(
        "/api/memory-settings", headers=headers, json={"extraction_enabled": False},
    )
    assert settings1.status_code == 200
    assert settings1.json() == {"extraction_enabled": False, "recall_enabled": True}
    settings2 = client.patch(
        "/api/memory-settings", headers=headers, json={"recall_enabled": False},
    )
    assert settings2.status_code == 200
    assert settings2.json() == {"extraction_enabled": False, "recall_enabled": False}
    assert client.get("/api/memory-settings", headers=headers).json() == settings2.json()
    assert client.get(f"/api/memories/{first.id}", headers=headers).status_code == 200
    assert client.get(f"/api/memories/{second.id}", headers=headers).status_code == 200

    bulk = client.post(
        "/api/memories/bulk-delete", headers=headers,
        json={"kind": "semantic", "confirmation": "DELETE"},
    )
    assert bulk.status_code == 200, bulk.text
    assert bulk.json()["affected_count"] == 2
    assert client.get("/api/memories", headers=headers).json() == []

    recalled = await _seed_v2(client, _ALICE, "被召回但不回显的正文")
    state = client.app.state.agent
    await state.ensure_stores()
    session_id = "recall-explanation-session"
    state.store.append_event(session_id, SessionEvent(
        seq=0, type=MEMORY_RECALLED, session_id=session_id, run_id="run-1",
        data={"memories": [{
            "memory_id": recalled.id, "version": recalled.version,
            "content": "不可信的事件正文", "ranking": {
                "ranking_version": "hybrid-v1", "score": 0.91,
                "private_note": "不允许透传",
            },
        }]},
    ))
    explanation = client.get(
        f"/api/sessions/{session_id}/memory-recalls", headers=headers,
    )
    assert explanation.status_code == 200, explanation.text
    payload = explanation.json()[0]
    assert payload["memories"][0]["memory_id"] == recalled.id
    assert payload["memories"][0]["ranking"] == {
        "ranking_version": "hybrid-v1", "score": 0.91,
    }
    assert "不可信的事件正文" not in explanation.text
    assert "不允许透传" not in explanation.text
    assert client.get(
        f"/api/sessions/{session_id}/memory-recalls", headers=_auth(_BOB),
    ).json() == []
