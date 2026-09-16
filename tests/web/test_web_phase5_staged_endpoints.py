"""Ticket B1：Context / Agent / Reasoning 三档只读清单端点（SDD 03 §10/§16 对齐）。

背景：Phase 5（df03990）已把 reasoning_effort / agent_profile / context_providers
四个字段作为 **staged 契约**接收（API 边界验证通过、运行时记一条 INFO 日志后忽略）。
前端 Phase 2b Composer control row 需要这三个字段的**清单端点**才能做选档 UI。

本测试覆盖三个新端点 × 三条 case：
  ① 正常返回已知值；
  ② 空目录降级（context-providers 当前可能返 []）；
  ③ 字段 schema 锁定（契约形态稳定，前端可放心消费）。

契约形态对齐既有 /api/permission-modes（封闭枚举：{id, display_name, description}）
与 /api/capabilities（动态列表，未装配就是空，不伪造）。Reuse First（§6）。
Scope Lock（§8）：本测试只锁清单端点契约，不验运行时消费（那是独立批次）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web import app as web_app
from agent_harness.web.app import CATALOG_ICON_NAMES, create_app


@pytest.fixture
def bare_client(tmp_path):
    """无外部依赖的 app + client（对齐 test_web_phase2_endpoints 的 bare_client）。"""
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        enable_cors=False,
    )
    return TestClient(create_app(settings, enable_cors=False))


# ── GET /api/reasoning-efforts ──


class TestReasoningEfforts:
    def test_returns_three_known_efforts(self, bare_client):
        """返回 Phase 5 validator 已锁定的三个档位：minimal / standard / deep。"""
        resp = bare_client.get("/api/reasoning-efforts")
        assert resp.status_code == 200
        body = resp.json()
        efforts = body["efforts"]
        ids = [e["id"] for e in efforts]
        # 已知值必须完整（与 CreateSessionRequest._validate_reasoning_effort 同集合）
        assert ids == ["minimal", "standard", "deep"]
        for e in efforts:
            assert e["display_name"], "每个 effort 必须有 display_name"
            assert e["description"], "每个 effort 必须有 description"

    def test_schema_locked(self, bare_client):
        """字段 schema 锁定：每条 entry 必须含 id / display_name / description / icon。"""
        resp = bare_client.get("/api/reasoning-efforts")
        body = resp.json()
        for e in body["efforts"]:
            assert set(e.keys()) == {"id", "display_name", "description", "icon"}
            assert isinstance(e["id"], str)
            assert isinstance(e["display_name"], str)
            assert isinstance(e["description"], str)

    def test_id_set_matches_validator(self, bare_client):
        """端点返回的 id 集合必须与 POST /api/sessions validator 接受的集合完全一致。
        不多不少——多了会让前端提交一个后端会 422 的值，少了会让前端少一个合法选项。
        """
        resp = bare_client.get("/api/reasoning-efforts")
        ids = {e["id"] for e in resp.json()["efforts"]}
        assert ids == {"minimal", "standard", "deep"}


# ── GET /api/agent-profiles ──


class TestAgentProfiles:
    def test_returns_three_known_profiles(self, bare_client):
        """返回 Phase 5 validator 已锁定的三个 profile：main / coding / research_review。"""
        resp = bare_client.get("/api/agent-profiles")
        assert resp.status_code == 200
        body = resp.json()
        profiles = body["profiles"]
        ids = [p["id"] for p in profiles]
        # 已知值必须完整（与 CreateSessionRequest._validate_agent_profile 同集合）
        assert ids == ["main", "coding", "research_review"]
        for p in profiles:
            assert p["display_name"], "每个 profile 必须有 display_name"
            assert p["description"], "每个 profile 必须有 description"

    def test_schema_locked(self, bare_client):
        """字段 schema 锁定：每条 entry 必须含 id / display_name / description / icon / tool_scope。"""
        resp = bare_client.get("/api/agent-profiles")
        body = resp.json()
        for p in body["profiles"]:
            assert set(p.keys()) == {
                "id", "display_name", "description", "icon", "tool_scope",
            }
            assert isinstance(p["id"], str)
            assert isinstance(p["display_name"], str)
            assert isinstance(p["description"], str)

    def test_id_set_matches_validator(self, bare_client):
        """端点返回的 id 集合必须与 POST /api/sessions validator 接受的集合完全一致。"""
        resp = bare_client.get("/api/agent-profiles")
        ids = {p["id"] for p in resp.json()["profiles"]}
        assert ids == {"main", "coding", "research_review"}

    def test_tool_scope_counts_match_declared_scopes(self, bare_client):
        """#201 AC：N/M 与 `BUILTIN_PROFILES[*].tool_scope` **逐值相等**，且互相对账。

        钉在这里的意图：这三个数一旦漂移（有人改了 scope 却没改口径、或有人把
        ``total`` 换成另一个来源），UI 上那句提示立刻变成假话，而**看是看不出来的**
        （数字只会变成另一个数字）。

        ⚠ 下面**显式写死 17 / 12 / 7**（而不是只跟 `BUILTIN_PROFILES` 自比）：前端
        `e2e/fixtures.ts::AGENT_PROFILES` 与 `lib/agentProfileScope.test.ts` 是这三
        个数的**手工镜像**，跨语言没有共享来源。只自比的话，某人往 `_CODING_TOOLS`
        加一个工具 → 后端测试照样绿、前端照样绿，而 fixture 里还是 12/17（真机是
        13/18）——"三处都绿、UI 说错话"正是这类漂移的形态。写死数字让后端先红，
        见到红请同时改前端那两处（这是有意的双份维护，与 §15 的 CSS 双份同理）。
        """
        from agent_harness.agent.profiles import (
            BUILTIN_PROFILES,
            declared_tool_universe,
        )

        universe = declared_tool_universe()
        resp = bare_client.get("/api/agent-profiles")
        by_id = {p["id"]: p["tool_scope"] for p in resp.json()["profiles"]}

        # 两处事实源必须同键集：`AGENT_PROFILE_DESCRIPTIONS`（文案，web 层）与
        # `BUILTIN_PROFILES`（工具面，agent 层）。少一个键 = 端点 KeyError 500，
        # 多一个键 = 前端出现一个选了就 422 的档位。
        assert set(by_id) == set(BUILTIN_PROFILES)
        for profile, scope in by_id.items():
            spec_scope = BUILTIN_PROFILES[profile].tool_scope
            assert scope["open"] == len(spec_scope)
            assert scope["total"] == len(universe)
            # excluded 就是并集减去本档位——**不是**另一个数，也不是"总减已开"
            # （那两个数在将来某个 scope 不在 main 里时会分叉）。
            assert scope["excluded"] == sorted(universe - spec_scope)
            assert (scope["open"] + len(scope["excluded"])) == scope["total"]

        # 手工镜像的数字（前端 fixture / 纯函数单测里各有一份），改动必须三处同步
        assert by_id["main"] == {"open": 17, "total": 17, "excluded": []}
        assert by_id["coding"]["open"] == 12
        assert by_id["coding"]["total"] == 17
        assert by_id["research_review"]["open"] == 7

    def test_main_profile_reports_nothing_narrowed(self, bare_client):
        """main（通用）**没有**被收窄的工具 ⇒ excluded 为空、open == total。

        前端据此不渲染那句提示（"从简、不能突兀"）：没被收窄就没有事实可说，
        「共 17 个中开放 17 个」只增噪音。
        """
        resp = bare_client.get("/api/agent-profiles")
        main = next(p for p in resp.json()["profiles"] if p["id"] == "main")
        assert main["tool_scope"]["excluded"] == []
        assert main["tool_scope"]["open"] == main["tool_scope"]["total"]

    def test_narrowed_profile_names_the_dropped_tools(self, bare_client):
        """收窄的档位必须**点名**掉了哪些工具（hover 提示的唯一数据源）。

        research_review 是只读档：write / edit / apply_patch / bash 必须在
        excluded 里——#198 的真实现象就是"选了档位后模型说没有 write/edit"。
        """
        resp = bare_client.get("/api/agent-profiles")
        research = next(
            p for p in resp.json()["profiles"] if p["id"] == "research_review"
        )
        excluded = set(research["tool_scope"]["excluded"])
        assert {"write", "edit", "apply_patch", "bash"} <= excluded
        # 有序（前端只取前 6 个 + 「…」，顺序必须稳定，否则每次打开提示都在变）
        assert research["tool_scope"]["excluded"] == sorted(excluded)


# ── GET /api/context-providers ──


class TestContextProviders:
    def test_empty_by_default_is_honest(self, bare_client):
        """默认未装配任何 context provider → 返回 [] （诚实降级，不伪造）。

        与 /api/capabilities 同原则：没有装配就不编条目，前端据空列表自行 fallback。
        （注意 `/api/capabilities` 现在**不是**空的——它有恒在的 core 条目，见 #193；
        空列表那条路径留给了"真的什么都没装配"的清单端点。）
        """
        resp = bare_client.get("/api/context-providers")
        assert resp.status_code == 200
        body = resp.json()
        # 当前 runtime 尚未装配任何 provider → 诚实返空数组
        assert body == {"providers": []}

    def test_schema_locked(self, bare_client):
        """顶层 key 锁定为 'providers'，值为 list（即便为空）。"""
        resp = bare_client.get("/api/context-providers")
        body = resp.json()
        assert "providers" in body
        assert isinstance(body["providers"], list)

    def test_top_level_key_stable(self, bare_client):
        """顶层 key 契约稳定：'providers'（复数，与 capabilities/modes 同模式）。

        不用 'context_providers' 长名——保持既有端点的短 key 风格（models/modes/
        capabilities/efforts/profiles/providers）。
        """
        resp = bare_client.get("/api/context-providers")
        body = resp.json()
        assert set(body.keys()) == {"providers"}


# ── #214：三个目录端点的 icon 语义名 ──


class TestCatalogIcons:
    """#214：三个目录端点在内置条目上下发**稳定**的 `icon` 名。

    锁三件事：
    ① 逐值稳定——把 `read-only` 的图标名从 `lock` 改成别的，前端那一行的字形会
       **静默变空**（未知名留空槽，不报错），所以必须逐条钉住，不能只断言"有值"；
    ② 下发名全部 ∈ `CATALOG_ICON_NAMES`（稳定取值集，不是自由字符串）；
    ③ 该集合与三个目录**实际下发的并集相等**——多一个名 = 文档里有但没人用，
       少一个 = 有人下发了一个没登记的名。
    """

    def test_icon_names_are_declared_and_exact(self, bare_client):
        modes = bare_client.get("/api/permission-modes").json()["modes"]
        efforts = bare_client.get("/api/reasoning-efforts").json()["efforts"]
        profiles = bare_client.get("/api/agent-profiles").json()["profiles"]

        emitted = ({m["icon"] for m in modes}
                   | {e["icon"] for e in efforts}
                   | {p["icon"] for p in profiles})
        assert emitted == CATALOG_ICON_NAMES

        assert {m["id"]: m["icon"] for m in modes} == {
            "read-only": "lock",
            "workspace-write": "pencil",
            "danger-full-access": "unlock",
        }
        assert {e["id"]: e["icon"] for e in efforts} == {
            "minimal": "bolt",
            "standard": "gauge",
            "deep": "telescope",
        }
        assert {p["id"]: p["icon"] for p in profiles} == {
            "main": "layers",
            "coding": "code",
            "research_review": "search",
        }

    def test_entry_without_icon_yields_null(self, bare_client, monkeypatch):
        """没有 icon 的条目 → `null`（前端留空槽），**不得**回落成 id、也不得编一个名。

        这是"部署/后端扩展目录"的未来路径（#214 要解决的正是它）：加一条不带 icon 的
        档位时载荷必须是 null，而不是让前端拿 id 去猜。
        """
        monkeypatch.setitem(
            web_app.REASONING_EFFORT_DESCRIPTIONS, "custom",
            {"display_name": "自定义档", "description": "部署自带档位"},
        )
        efforts = bare_client.get("/api/reasoning-efforts").json()["efforts"]
        [custom] = [e for e in efforts if e["id"] == "custom"]
        assert custom["icon"] is None
