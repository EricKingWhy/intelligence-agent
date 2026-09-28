"""T3（#308）：`budget.local.max_agent_turns` 的 HTTP 语义 + "被拒请求零副作用"。

车票 AC 里属于 HTTP 边界的那几条（`#308` AC + `#320` contract 收口）：

- 新字段 / 越权配置 / 已退役 alias **都有测试**；
- 越权与退役字段必须在 model / tool / session run **启动前**失败（`R4`：拒绝，不静默截断）；
- 旧客户端发 `max_steps` ⇒ **未知字段 422**（`extra="forbid"`），不再有 deprecation 通道；
- effective local fuse 要有**只读投影**（Must Do：供客户端显示，`11 §6.1`）。

判定规则本身住在 `agent_harness.agent.budget.resolve_local_fuse`（单一规则来源），
其穷举单测在 `tests/agent/test_local_budget_resolution.py`。本文件**不重测规则**，
只锁 HTTP 层三件事：

1. 422 的映射（`web/domain_errors.py` 里 `BudgetRejection` 家族 → 422）+ 退役字段
   的未知键 422（pydantic `extra="forbid"`）；
2. 投影通道（响应头 `X-Local-Max-Agent-Turns` / `X-Local-Fuse-Source`）；
3. **零副作用**：被拒请求不建 workspace、不落 session JSONL、不构造模型
   （"启动 run 前失败"的可观测定义——三样都动不了，run 自然没启动）。

WS 帧（第四个续聊入口）单独覆盖：形状错误与语义拒绝都必须回错误帧，
不能静默丢弃（`web/websocket.py::ws_budget_claims`）。
"""

from __future__ import annotations

from typing import Any, Self
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel

_DEFAULT_CEILING = 500


def _web(tmp_path, *, ceiling: int | None = None) -> tuple[Any, TestClient]:
    """隔离 app + client；`ceiling` 显式模拟运维压低 Deployment 上限。"""
    overrides: dict[str, Any] = {
        "_env_file": None,  # 不吃仓库根 .env（测试必须自足）
        "workspace_dir": str(tmp_path),
        "model_api_key": "sk-test",
        "enable_cors": False,
    }
    if ceiling is not None:
        overrides["local_max_agent_turns"] = ceiling
    app = create_app(Settings(**overrides), enable_cors=False)
    return app, TestClient(app)


class _ModelProbe:
    """模型工厂探针：既提供确定性剧本，又记录"模型真的被构造过"。

    "启动 run 前失败"不能只靠状态码证明——被拒请求若已经构造了模型，说明
    校验点在副作用之后。`calls` 为空的断言就是那条边界的可观测证据。
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self._patcher: Any = None

    def __enter__(self) -> Self:
        def _factory(config: Any, **kwargs: Any) -> ScriptedModel:
            self.calls.append(config)
            return ScriptedModel(responses=[AIMessage(content="ok")])

        self._patcher = patch(
            "agent_harness.assembly.create_chat_model", side_effect=_factory,
        )
        self._patcher.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        self._patcher.stop()
        return False


def _assert_rejected_without_side_effects(app: Any, probe: _ModelProbe) -> None:
    """被拒请求：不建 workspace、不落 session JSONL、不构造模型。"""
    assert probe.calls == [], "被拒请求不得构造模型（校验必须早于任何工作）"
    assert list(app.state.agent.workspaces_root.iterdir()) == [], "不得创建 workspace"
    assert list(app.state.agent.sessions_root.iterdir()) == [], "不得落盘 session"


# ── 接受路径：投影 ──


def test_default_request_resolves_to_deployment_ceiling(tmp_path):
    """缺省请求（不带任何 fuse 字段）→ 生效 500、来源 deployment。"""
    _, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post("/api/sessions", json={"task": "hi"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == str(_DEFAULT_CEILING)
    assert resp.headers["x-local-fuse-source"] == "deployment"
    assert "deprecation" not in resp.headers, "没用旧字段就不该报废弃"
    assert probe.calls, "合法请求必须真的构造模型（否则上面的头只是装饰）"


def test_new_field_only_accepted_without_deprecation_signal(tmp_path):
    """新字段-only → 接受，来源是请求字段本身，无 deprecation。"""
    _, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"local": {"max_agent_turns": 6}}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == "6"
    assert resp.headers["x-local-fuse-source"] == "budget.local.max_agent_turns"
    assert "deprecation" not in resp.headers
    assert "warning" not in resp.headers


def test_retired_alias_rejected_as_unknown_field(tmp_path):
    """#320 contract：旧字段单独出现 ⇒ 未知字段 422（不是静默解释、也不是 deprecation）。"""
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post("/api/sessions", json={"task": "hi", "max_steps": 7})
    assert resp.status_code == 422, resp.text
    assert "max_steps" in resp.text, "错误要指名被拒的字段"
    _assert_rejected_without_side_effects(app, probe)


def test_retired_alias_next_to_new_field_still_rejected(tmp_path):
    """双字段同发（无论相等与否）⇒ 422：未知键在形状层就被拒，没有"恰好相等"的豁免。"""
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "max_steps": 8,
                "budget": {"local": {"max_agent_turns": 8}},
            },
        )
    assert resp.status_code == 422, resp.text
    _assert_rejected_without_side_effects(app, probe)


def test_request_equal_to_deployment_ceiling_accepted(tmp_path):
    """边界：恰好等于 ceiling 是收紧到顶，不是越权（无 off-by-one）。"""
    _, client = _web(tmp_path, ceiling=5)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"local": {"max_agent_turns": 5}}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == "5"


# ── 拒绝路径：422 + 零副作用 ──


def test_alias_and_unequal_budget_rejected_before_any_work(tmp_path):
    """不等双字段 → 422（未知键判据，先于一切语义合并），且发生在任何工作开始之前。"""
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "max_steps": 8,
                "budget": {"local": {"max_agent_turns": 9}},
            },
        )
    assert resp.status_code == 422, resp.text
    assert "max_steps" in resp.text, f"错误要指名退役字段：{resp.text}"
    _assert_rejected_without_side_effects(app, probe)


def test_request_over_deployment_ceiling_rejected_before_any_work(tmp_path):
    """越权（request > Deployment ceiling）→ 422，且不启动任何工作。"""
    app, client = _web(tmp_path, ceiling=5)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"local": {"max_agent_turns": 6}}},
        )
    assert resp.status_code == 422, resp.text
    assert "5" in resp.json()["detail"], "错误文案要给出生效 ceiling"
    _assert_rejected_without_side_effects(app, probe)


def test_alias_never_reaches_the_ceiling_rule_anymore(tmp_path):
    """退役字段连 ceiling 判据都到不了：形状层 422（不再有"同一语义"的迁移期通道）。"""
    app, client = _web(tmp_path, ceiling=5)
    probe = _ModelProbe()
    with probe:
        resp = client.post("/api/sessions", json={"task": "hi", "max_steps": 6})
    assert resp.status_code == 422, resp.text
    assert "ceiling" not in resp.text, "拒绝理由是未知字段，不是越权"
    _assert_rejected_without_side_effects(app, probe)


def test_request_over_profile_ceiling_rejected_before_any_work(tmp_path, monkeypatch):
    """PRD 决策 3 / R3：请求不得越过 **AgentProfile 政策**——生效上层 = min(各层)。

    内置三档位都声明 `None`（出厂不写死数字），所以这里把一个档位的声明值调到 40：缺省
    Deployment 500 仍在上，**档位**才是生效上层，请求 300 必须 422（而不是静默取 40）。
    请求 30 是合法的收窄 ⇒ 200 且投影回 30。
    """
    from dataclasses import replace

    from agent_harness.agent.profiles import BUILTIN_PROFILES

    monkeypatch.setitem(
        BUILTIN_PROFILES, "coding",
        replace(BUILTIN_PROFILES["coding"], max_agent_turns=40),
    )
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        rejected = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "agent_profile": "coding",
                "budget": {"local": {"max_agent_turns": 300}},
            },
        )
    assert rejected.status_code == 422, rejected.text
    assert "40" in rejected.json()["detail"], "错误文案要给出生效 ceiling（档位声明）"
    _assert_rejected_without_side_effects(app, probe)

    with probe:
        accepted = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "agent_profile": "coding",
                "budget": {"local": {"max_agent_turns": 30}},
            },
        )
    assert accepted.status_code == 200, accepted.text
    assert accepted.headers["x-local-max-agent-turns"] == "30"


def test_steer_judges_the_budget_body_before_the_target_check(tmp_path):
    """非启动分支（steer）也要判请求体，且**判定先于**"有没有在途 run"这条分支。

    空转会话上 `mode=steer` 本来是 409（`SteerTargetNotFound`）；请求体自身矛盾时
    必须是 **422**。若判定被放到分支之后，这里读到的就是 409——同一份 body 的语义
    取决于运行态，客户端与复核者都无从预期。

    "在途 run 的 queued / steer" 那条分支需要把 run 真钉在在途（本文件的
    `TestClient` 会等 SSE 流跑完，拿不到那个窗口），证据在会话层：
    `tests/session/test_multiturn_delivery.py::test_in_flight_input_still_judges_the_budget_body`。
    """
    from agent_harness.session.event import MESSAGE_QUEUED, STEER_REQUESTED

    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    before = [e.type for e in app.state.agent.store.read_events(session_id)]

    retired = client.post(
        f"/api/sessions/{session_id}/messages",
        json={
            "content": "you are confused",
            "mode": "steer",
            "max_steps": 8,
            "budget": {"local": {"max_agent_turns": 9}},
        },
    )
    assert retired.status_code == 422, retired.text
    assert "max_steps" in retired.text, f"错误要指名退役字段：{retired.text}"

    over_ceiling = client.post(
        f"/api/sessions/{session_id}/messages",
        json={
            "content": "you are confused",
            "mode": "steer",
            "budget": {"local": {"max_agent_turns": 10_000}},
        },
    )
    assert over_ceiling.status_code == 422, over_ceiling.text

    # 被拒请求零副作用（会话已存在，所以这里看事件流而不是 workspace）：事件流**逐条**
    # 不变——只断言"某两个类型不在列表里"会放过 queue/cancelled、message/superseded
    # 这类被拒请求本不该写下的记录。
    after = [e.type for e in app.state.agent.store.read_events(session_id)]
    assert after == before, f"{before} → {after}"
    assert MESSAGE_QUEUED not in after and STEER_REQUESTED not in after

    # 对照组：合法 body 在同一个空转会话上仍是 409——判定没把目标检查吞掉，
    # 也没把"没在途 run"这件事实改写成别的状态码。
    legal = client.post(
        f"/api/sessions/{session_id}/messages",
        json={
            "content": "you are confused",
            "mode": "steer",
            "budget": {"local": {"max_agent_turns": 9}},
        },
    )
    assert legal.status_code == 409, legal.text


@pytest.mark.parametrize("turns", [0, -1])
def test_non_positive_turns_rejected_before_any_work(tmp_path, turns):
    """非正数 → 422（形状闸门在 pydantic，领域层还有一层 `_positive`）。"""
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"local": {"max_agent_turns": turns}}},
        )
    assert resp.status_code == 422, resp.text
    _assert_rejected_without_side_effects(app, probe)


def test_unknown_key_in_budget_rejected_not_silently_ignored(tmp_path):
    """budget 里的未知键 → 422。

    拼错字段名若被静默忽略，用户会以为自己设了预算——真正的上限仍是 500。
    这类"以为设了"比 422 危险得多，所以 `extra="forbid"`。
    """
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "budget": {"local": {"max_agent_turns": 5, "max_turn": 5}},
            },
        )
    assert resp.status_code == 422, resp.text
    _assert_rejected_without_side_effects(app, probe)


def test_unimplemented_scopes_and_dimensions_not_accepted_yet(tmp_path):
    """尚未实现的作用域 / 维度一律 422（T4 开 turns、T5 `#313` 再开 requests / tokens、
    T6 `#314` 再开 per-tool 配额、T7 `#315` 再开 deadline）。

    "先看起来接受、其实不生效"是最坏的一种兼容：用户会以为预算在管。宁可
    显式拒绝——本用例钉住**仍未实现**的维度（PRD §3 冻结形状里 run 的
    `max_tool_calls` 属后续票）加上 CAS 版本的**入口形状规则**：

      * `budget.run` 里仍未实现的维给了**非空值** ⇒ 422（给了 `null` / `{}` 则合法，
        见 `tests/web/test_run_pause_resume_api.py` 的 PRD 全形用例）；
      * `budget.expected_version` 在创建入口 ⇒ 422（创建会启动新 run，没有版本可比）；
      * `budget.session.expected_version` 在创建入口 ⇒ 422（`#318`：session 账行随
        首个 run 才建出，新建会话没有可比较的行版本）。

    `budget.session` 的**各维**自 `#318` 起已实现（正控在下面）：它不再出现在拒绝
    名单里；它的 CAS 规则与 run 账不同——账行跨 run 存续，所以 /resume、/messages
    等面向已存在会话的入口带版本合法（领域判定见 service / 账本，409 零副作用）。

    `max_cost_usd` 留在拒绝名单里的理由与上面几类**不同**（`#313` 已声明它的形状）：
    本链的 Provider 集成不自报归属成本 ⇒ 这条 ceiling 现在无法强制执行 ⇒ 按
    `11 §6.1` 在首个请求前 422（判定见 `agent/run_budget.validate_ceiling_enforceability`），
    而不是收下一个永远不会触发的数字。session 作用域的 `max_cost_usd` 同判
    （同一份账目能力声明），正控里给的是 turns 维度。

    `tool_call_limits` 自 `#314` 起**已实现**，所以从本名单移出——它的形状与"名字已
    注册"两条判定各有自己的用例（后者见下一个用例，那里同时钉住它的副作用边界）。

    `deadline_at` 自 `#315` 起**已实现**，同样移出：它的形状判定在
    `tests/agent/test_run_budget.py`（朴素时间 / 空串 / 非字符串 ⇒ 422），接收面的
    行为（**已过去的时刻合法且立刻到点** ⇒ 200 + 即时 `run/paused`）见
    `tests/web/test_run_pause_resume_api.py::test_deadline_in_the_past_pauses_immediately`。
    """
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        for payload in (
            {"budget": {"run": {"max_tool_calls": 5}}},
            {"budget": {"run": {"max_cost_usd": "0.01"}}},
            {"budget": {"expected_version": 3}},
            {"budget": {"session": {"expected_version": 3}}},
        ):
            resp = client.post("/api/sessions", json={"task": "hi", **payload})
            assert resp.status_code == 422, f"{payload} 应被拒：{resp.text}"
    _assert_rejected_without_side_effects(app, probe)
    with probe:
        # 正控（`#318`）：`budget.session` 各维已实现——显式声明被接受。`launch=false`
        # 走 **query 参数**（与 `_create_idle_session` 同一条通道；#320 的 `extra="forbid"`
        # 顺带揪出本用例曾把 `launch` 放进 body——那个键从未被模型声明、一直被静默
        # 忽略，"只建会话"的注释声称与实际行为不符）。launch=false 只建会话不启动
        # run，账行自然还没建出；这里只钉"请求形状不再被拒"。
        ok = client.post(
            "/api/sessions",
            json={
                "budget": {"session": {"max_agent_turns_total": 5,
                                       "max_delegations": 3}},
            },
            params={"launch": "false"},
        )
    assert ok.status_code == 200, ok.text


def test_tool_call_limits_requires_registered_names(tmp_path):
    """`budget.run.tool_call_limits` 的工具名必须**已注册**（`#314` / `04 §9.1`）⇒ 422。

    给一个本 run 调不到的名字配配额是**请求本身**有问题（客户端以为它在限制什么），
    所以拒绝整个请求，而不是运行期静默忽略（ADR-0044 D1/D8 的"不静默截断"）。

    副作用边界比形状级拒绝**松一档**，且这是刻意的（登记在 ADR-0045）：注册表是
    装配层的产物（内置 + artifact 读回 + capability，再按 profile 收窄），"哪些工具
    已注册"在 `build_runtime` 之前没有事实可言 ⇒ 这条判定只能落在注册表定型处。
    它的保证是 `11 §6.1` 的原话——**不发起任何 Provider 请求、不执行任何工具、
    不启动任何 child、不落任何消耗预算的事件**（本用例用"连 session 都没落盘"来钉：
    没有 session 就没有任何事件可言）；工作目录与模型**对象**此时已存在，它们不是
    Provider 请求。别把这条判定想成与形状级拒绝同级——形状非法时连工作目录都没有。
    """
    app, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"run": {"tool_call_limits": {"read_file": 3}}}},
        )
    assert resp.status_code == 422, resp.text
    assert "未注册" in resp.text, resp.text
    assert list(app.state.agent.sessions_root.iterdir()) == [], "不得落盘 session"

    # 正控：换成**已注册**的工具名（内置只读工具 `read`）⇒ 接受并真跑起来
    with probe:
        ok = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"run": {"tool_call_limits": {"read": 3}}}},
        )
    assert ok.status_code == 200, ok.text
    assert probe.calls, "合法请求必须真的构造模型"


# ── resume / messages：同一条通道 ──


def _create_idle_session(client: TestClient) -> str:
    """只建会话不启动 run（launch=false）→ 得到可 resume 的空闲会话。"""
    resp = client.post("/api/sessions", json={}, params={"launch": "false"})
    assert resp.status_code == 200, resp.text
    # 只建会话**不投影** fuse：那个数字是请求级的、未被任何 run 消费，也不持久化
    # （后续 /messages 会按当时的 Deployment 重新解析）——回一个"会话级 ceiling"
    # 是假事实（不变量 #21 同族：缺失不能被顶替成看起来有值）。
    assert "x-local-max-agent-turns" not in resp.headers, resp.headers
    assert "x-local-fuse-source" not in resp.headers, resp.headers
    return resp.json()["session_id"]


def test_queue_flush_projects_the_same_fuse(tmp_path):
    """队列重投（flush）的 launched 响应与 `/messages` 同一投影（同一段组装）。

    起点是**空会话**（`launch=false`）+ 手工 append 一条 `message/queued`（模拟"重启后
    手工投递"）：先跑一个真 run 再 append 会与终态回调的自动接力竞争（既有用例
    `test_multiturn_queue_http.py` 已记录过这个形状）。
    """
    from agent_harness.session import Session
    from agent_harness.session.event import MESSAGE_QUEUED
    from agent_harness.session.store import JsonlSessionStore

    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    Session.append_event(
        JsonlSessionStore(root=app.state.agent.sessions_root), session_id,
        MESSAGE_QUEUED, {"queue_id": "q-flush", "content": "重启前的消息"},
    )
    probe = _ModelProbe()
    with probe:
        resp = client.post(f"/api/sessions/{session_id}/queue/flush")
    assert resp.status_code == 200, resp.text
    assert probe.calls, "flush 必须真的拉起 run（否则响应头只是装饰）"
    assert resp.headers["x-local-max-agent-turns"] == str(_DEFAULT_CEILING)
    assert resp.headers["x-local-fuse-source"] == "deployment"


def test_resume_honors_budget_and_projects_fuse(tmp_path):
    """显式恢复也接受 budget（`11 §6.1`），投影与创建路径同形。"""
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={"task": "继续", "budget": {"local": {"max_agent_turns": 9}}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == "9"
    assert resp.headers["x-local-fuse-source"] == "budget.local.max_agent_turns"


def test_resume_rejects_retired_alias_too(tmp_path):
    """退役规则是全局的（`02 §5.1` D8 的收口面）：恢复端点同样 422，不留静默通道。"""
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/resume", json={"task": "继续", "max_steps": 9},
        )
    assert resp.status_code == 422, resp.text
    assert "max_steps" in resp.text
    assert probe.calls == []


def test_rejected_resume_appends_nothing(tmp_path):
    """被拒的 resume 不得追加 `session/resumed`（拒绝早于 Session.resume）。"""
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    before = [e["type"] for e in client.get(f"/api/sessions/{session_id}/events").json()]

    probe = _ModelProbe()
    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "task": "继续",
                "max_steps": 8,
                "budget": {"local": {"max_agent_turns": 9}},
            },
        )
    assert resp.status_code == 422, resp.text
    after = [e["type"] for e in client.get(f"/api/sessions/{session_id}/events").json()]
    assert after == before, "被拒请求不得改动会话历史"
    assert "session/resumed" not in after
    assert probe.calls == []


def test_send_message_launched_projects_fuse(tmp_path):
    """续聊的 launched 分支与创建路径共用同一条投影通道。"""
    _, client = _web(tmp_path)
    probe = _ModelProbe()
    with probe:
        created = client.post("/api/sessions", json={"task": "首轮"})
        assert created.status_code == 200, created.text
        session_id = client.get("/api/sessions").json()[0]["session_id"]
        resp = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "继续", "budget": {"local": {"max_agent_turns": 12}}},
        )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-local-max-agent-turns"] == "12"
    assert resp.headers["x-local-fuse-source"] == "budget.local.max_agent_turns"


# ── WS 帧：第四个续聊入口 ──


def test_ws_budget_shape_error_returns_error_frame(tmp_path):
    """WS 帧里的形状错误当场回错误帧，连接保持（不静默丢弃、不静默死掉）。"""
    import json

    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "type": "send_message",
            "session_id": session_id,
            "content": "继续",
            "budget": {"local": {"max_agent_turns": 5, "max_turn": 5}},
        }))
        payload = json.loads(ws.receive_text())
        assert payload["type"] == "error", payload
        ws.send_text(json.dumps({"type": "ping"}))
        assert json.loads(ws.receive_text())["type"] == "pong"


def test_ws_retired_alias_returns_error_frame(tmp_path):
    """WS 上的退役字段同样有回声（第四个入口不能绕过未知键拒绝）。"""
    import json

    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    before = [e["type"] for e in client.get(f"/api/sessions/{session_id}/events").json()]

    probe = _ModelProbe()
    with probe, client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "type": "send_message",
            "session_id": session_id,
            "content": "继续",
            "max_steps": 8,
            "budget": {"local": {"max_agent_turns": 9}},
        }))
        payload = json.loads(ws.receive_text())
    assert payload["type"] == "error", payload
    assert "max_steps" in payload["message"], payload
    after = [e["type"] for e in client.get(f"/api/sessions/{session_id}/events").json()]
    assert after == before, "被拒帧不得改动会话历史"
    assert probe.calls == []
