"""#1197 项 1 HTTP 面：`GET`/`PATCH /api/sessions/{id}/context-settings`。

**路由形态的偏离（用户裁决 A，2026-10-16 14:06）**：票面原文写的是
`PATCH /api/context-settings`「作用于当前会话（由请求会话上下文隐含，不带 session id
参数）」。该条按字面**不可实现**——服务端没有"当前会话"概念（`current_session_var`
只在 run 执行内绑定，REST 处理器里取不到），前端也不发任何 session 头。裁决改为复用
仓库统一 per-session 形状（先例：`POST /api/sessions/{id}/workflow-mode`、
`.../permission-mode`、`.../model`）。票面正文不动，语义记在代码与本文件。

**本文件钉的外部契约**：

- `GET /api/sessions/{id}/context-settings` → 200 `{session_id, context_window,
  tiers, thresholds:{auto_compact, hard_guard}, configured_default}`（只读，无来源闸）；
- `PATCH` 同路径，体 `{context_window: int}` → 200 同形状（**幂等**）；
- 挡位外 / 非整数 → 422 **且零写入**（先于 404 的 id 形态校验同 workflow-mode）；
- 无会话 → 404；id 非法 → 422；
- PATCH 有来源闸 `require_trusted_origin`（宿主侧管理动作，ADR-0025 D1）；
- 改完立刻在 `GET` 上可见，且**作用于路径里那个会话**（per-session 独立）。

黑盒：只经 HTTP + 直接读事件流。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import CONTEXT_WINDOW_TIERS, Settings
from agent_harness.session import Session
from agent_harness.web.app import create_app

_PATH = "/api/sessions/{sid}/context-settings"


def _client(tmp_path: Path, **overrides) -> TestClient:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test", **overrides
    )
    return TestClient(create_app(settings, enable_cors=False))


def _new_session(client: TestClient) -> str:
    """造一个**真实形态**的会话：事件流 + workspace 映射。

    只 `Session.start(store)` 是不够的——真实会话由 `POST /api/sessions` 建立时
    workspace 映射一定存在，所以这里补上 `create` 才是**忠实**的夹具（不是为迁就
    实现而放宽断言）。映射缺席的会话另有专测：`Session.load` 自 #1235 起降级为
    `sandbox=None`（`tests/web/test_1235_session_load_without_mapping.py`）；
    文件路由仍按 404 处理
    （`tests/web/test_workspace_files_api.py::test_session_without_workspace_mapping_is_404_not_500`）。
    """
    agent = client.app.state.agent
    session = Session.start(agent.store)
    agent.workspace_registry.create(
        session.session_id, workspace_root=Path(client.app.state.agent.settings.workspace_dir)
    )
    return session.session_id


def _get(client: TestClient, sid: str):
    return client.get(_PATH.format(sid=sid))


def _patch(client: TestClient, sid: str, body: dict):
    return client.patch(_PATH.format(sid=sid), json=body)


def _window_events(client: TestClient, sid: str) -> list[dict]:
    events = client.get(f"/api/sessions/{sid}/events").json()
    return [e for e in events if e["type"] == "context/window-changed"]


# ── GET ────────────────────────────────────────────────────────────────


def test_get_defaults_to_the_deployment_window(tmp_path):
    """没改过 ⇒ 部署级默认（Settings.max_context_tokens），不是写死的 200K。"""
    client = _client(tmp_path, max_context_tokens=300_000)
    sid = _new_session(client)

    body = _get(client, sid).json()

    assert body["session_id"] == sid
    assert body["context_window"] == 300_000
    assert body["configured_default"] == 300_000
    assert body["tiers"] == list(CONTEXT_WINDOW_TIERS)


def test_get_thresholds_are_derived_from_the_effective_window(tmp_path):
    """70%/85% 线随挡位换算——票面验收第一条的可观测面。"""
    client = _client(tmp_path)
    sid = _new_session(client)

    assert _patch(client, sid, {"context_window": 500_000}).status_code == 200
    body = _get(client, sid).json()

    assert body["context_window"] == 500_000
    assert body["thresholds"] == {"auto_compact": 350_000, "hard_guard": 425_000}
    # 十进制整数（票面「一律十进制整数」）：JSON 里必须是 int 而不是 350000.0
    assert all(isinstance(v, int) for v in body["thresholds"].values())


@pytest.mark.parametrize(
    ("window", "auto", "hard"),
    [(100_000, 70_000, 85_000), (200_000, 140_000, 170_000),
     (256_000, 179_200, 217_600), (300_000, 210_000, 255_000),
     (500_000, 350_000, 425_000)],
)
def test_thresholds_for_every_tier(tmp_path, window, auto, hard):
    client = _client(tmp_path)
    sid = _new_session(client)

    assert _patch(client, sid, {"context_window": window}).status_code == 200
    assert _get(client, sid).json()["thresholds"] == {
        "auto_compact": auto, "hard_guard": hard,
    }


# ── PATCH ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tier", CONTEXT_WINDOW_TIERS)
def test_patch_accepts_every_tier_and_persists_one_event(tmp_path, tier):
    client = _client(tmp_path)
    sid = _new_session(client)

    resp = _patch(client, sid, {"context_window": tier})

    assert resp.status_code == 200, resp.text
    assert resp.json()["context_window"] == tier
    events = _window_events(client, sid)
    assert [e["data"] for e in events] == [{"context_window": tier}]


def test_patch_is_idempotent_and_last_wins(tmp_path):
    """重复 PATCH 各落一条事件（append-only），派生取最后一条。"""
    client = _client(tmp_path)
    sid = _new_session(client)

    _patch(client, sid, {"context_window": 100_000})
    _patch(client, sid, {"context_window": 500_000})
    _patch(client, sid, {"context_window": 500_000})

    assert _get(client, sid).json()["context_window"] == 500_000
    assert [e["data"]["context_window"] for e in _window_events(client, sid)] == [
        100_000, 500_000, 500_000,
    ]


def test_patch_is_per_session(tmp_path):
    """**per-session 独立**（票面核心要求）：改 A 不动 B。"""
    client = _client(tmp_path)
    a, b = _new_session(client), _new_session(client)

    _patch(client, a, {"context_window": 100_000})

    assert _get(client, a).json()["context_window"] == 100_000
    assert _get(client, b).json()["context_window"] == 200_000
    assert _window_events(client, b) == []


# ── 校验：422 + 零写入 ─────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [100, 150_000, 256_001, 400_000, 1_000_000, 0, -1])
def test_patch_rejects_non_tier_without_writing(tmp_path, bad):
    client = _client(tmp_path)
    sid = _new_session(client)

    resp = _patch(client, sid, {"context_window": bad})

    assert resp.status_code == 422, resp.text
    assert _window_events(client, sid) == []
    assert _get(client, sid).json()["context_window"] == 200_000


@pytest.mark.parametrize("bad", ["200000", 200_000.5, None, True, [200_000]])
def test_patch_rejects_non_integer_without_writing(tmp_path, bad):
    client = _client(tmp_path)
    sid = _new_session(client)

    assert _patch(client, sid, {"context_window": bad}).status_code == 422
    assert _window_events(client, sid) == []


def test_patch_rejects_unknown_fields(tmp_path):
    """`extra="forbid"`（照抄 `MemorySettingsPatch`）：拼错字段名不静默吞掉。"""
    client = _client(tmp_path)
    sid = _new_session(client)

    assert _patch(client, sid, {"context_window": 100_000, "tier": "100k"}).status_code == 422


def test_patch_requires_the_window_field(tmp_path):
    client = _client(tmp_path)
    sid = _new_session(client)

    assert _patch(client, sid, {}).status_code == 422


# ── 404 / 422（id 形态）────────────────────────────────────────────────


def test_missing_session_is_404_on_both_verbs(tmp_path):
    client = _client(tmp_path)
    missing = "0" * 32

    assert _get(client, missing).status_code == 404
    assert _patch(client, missing, {"context_window": 100_000}).status_code == 404


@pytest.mark.parametrize("bad_id", ["has space", "bang!", "dot.dot", "a" * 129])
def test_invalid_session_id_is_422_on_patch(tmp_path, bad_id):
    """id 形态校验**先于** 404（`_require_existing_session` 的顺序即契约）。

    用例 id 都是"形态非法但可路由"（空格/`!`/点/超长 129）：它们能到达端点，
    必须被形态校验拦成 422，而不是落到"不存在"的 404——精确 `== 422` 才能抓住
    顺序回归（旧 `in (404, 422)` 对两者都放行）。
    `""` / `"../etc/passwd"` 走不到校验器（路由层直接 404），不属于本断言面，
    见 `test_path_traversal_id_is_404_on_patch`。
    """
    client = _client(tmp_path)

    resp = client.patch(f"/api/sessions/{bad_id}/context-settings",
                        json={"context_window": 100_000})

    assert resp.status_code == 422, resp.text


def test_path_traversal_id_is_404_on_patch(tmp_path):
    """路径穿越 id 在路由层即 404：`..` 段形不成合法路由，绝不逃逸到文件系统。

    注意这是传输层行为，不是形态校验器的 422——两者断言面不同，故独立成例。
    """
    client = _client(tmp_path)

    resp = client.patch("/api/sessions/../etc/passwd/context-settings",
                        json={"context_window": 100_000})

    assert resp.status_code == 404, resp.text
