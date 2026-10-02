"""#562（RL-04 深度 / F6 reason 长度）：wire 层请求体输入加固——红测。

两个已拍板数值合同（用户 2026-10-03）：

* **请求体 JSON 嵌套深度上限 = 100**（FUZZ-01 同族的 RL-04：深嵌套 ``RecursionError``）。
* ``POST /api/sessions/{id}/approve`` 的 ``reason`` 上限 = **2000 字符**（不是字节）。

## 深度判据（与实现同一口径）

``depth`` = 最深路径上的容器（object + array）层数，**最外层 body 容器记 1**。
请求体 ``{"n": [[…]]}``（外层对象 1 + N 层数组）的 depth = ``N + 1``。上限 100 ⇒
depth ≤ 100 放行，depth ≥ 101 拒绝。构造见 ``_deep_body``。

## 为什么错误面必须是 422（本票与「裸 400」的核心区别）

当前树（无守卫）深 body 走 ``fastapi/routing.py`` 的 ``await request.json()``：
depth≳3000 时 ``json.loads`` 抛 ``RecursionError``，被 ``routing.py`` 的
``except Exception`` 归成**裸 400** ``There was an error parsing the body``
（本轮实测：depth 1500 尚能解析 ⇒ 422 ``extra_forbidden``；depth 3000 ⇒ 400）——
后者与「显式配额拒绝」语义不符（audit：拒绝而非崩溃）。守卫落在**解析之前**，
用固定字符串信封回 422。

## 与上一轮只读侦察（recon562.md §7）的偏差（以本轮实测为准）

recon 计划用 ``detail[0].type == "body_too_deep"``（数组形态）。本轮按施工单
B 节「``detail`` 保持字符串契约」实现，故断言**字符串信封**（``ErrorEnvelope``，
与 ``http_error`` 的业务 422 同形，OpenAPI 422 已声明 oneOf 覆盖两种形态）。
判别式因此改为：守卫命中 ⇒ ``detail`` 是**字符串**且等于 ``BODY_TOO_DEEP_DETAIL``；
放行 ⇒ ``detail`` 是**数组**（pydantic ``extra_forbidden``）。两种形态互斥，可判真伪。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app

JSON_HDR = {"content-type": "application/json"}

#: 深度配额 422 的 ``detail`` **字符串契约**（字面量钉在这里，另由
#: ``test_quota_detail_string_is_stable`` 与实现常量对账，杜绝两侧漂移）。
_EXPECTED_QUOTA_DETAIL = "request body nesting too deep"

#: 合法最大嵌套：`budget.session.tool_call_limits`（depth=4，实测当前树 200）。
_LEGAL_MAX_DEPTH_BODY = {"budget": {"session": {"tool_call_limits": {"bash": 3}}}}


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return TestClient(create_app(settings, enable_cors=False))


def _deep_body(depth: int) -> bytes:
    """构造总容器深度恰为 ``depth`` 的 body：``{"n": [[…0…]]}``。

    depth = 1（外层对象）+ ``depth - 1`` 层数组。
    """
    assert depth >= 1
    inner = depth - 1
    return b'{"n": ' + b"[" * inner + b"0" + b"]" * inner + b"}"


def _post_raw(client: TestClient, path: str, raw: bytes):
    return client.post(path, content=raw, headers=JSON_HDR)


def _session_dirs(tmp_path: Path) -> set[str]:
    root = tmp_path / "sessions"
    if not root.exists():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir()}


def _is_quota_422(resp) -> bool:
    """守卫命中的判别式：422 + ``detail`` 是等于配额的**字符串**信封。"""
    return resp.status_code == 422 and resp.json().get("detail") == _EXPECTED_QUOTA_DETAIL


def test_quota_detail_string_is_stable() -> None:
    """实现常量必须等于测试钉住的字符串契约（单边改名 ⇒ 此例红）。"""
    from agent_harness.web.wire_safety import BODY_TOO_DEEP_DETAIL

    assert BODY_TOO_DEEP_DETAIL == _EXPECTED_QUOTA_DETAIL


# ── 1. 深度：上限 100 ⇒ 98/99/100 放行，101 拒绝 ─────────────────────────


@pytest.mark.parametrize("depth", [98, 99, 100])
def test_depth_at_or_below_limit_passes_guard(tmp_path: Path, depth: int) -> None:
    """98/99/100 **必须通过守卫**：落到 pydantic，``detail`` 是数组（``extra_forbidden``），
    不是配额字符串信封。"""
    client = _client(tmp_path)
    resp = _post_raw(client, "/api/sessions?launch=false", _deep_body(depth))
    assert resp.status_code == 422, f"depth={depth} {resp.status_code} {resp.text[:200]}"
    detail = resp.json()["detail"]
    assert isinstance(detail, list), f"depth={depth} 守卫不应命中：{detail!r}"
    assert detail != _EXPECTED_QUOTA_DETAIL


def test_depth_101_is_refused_by_quota_guard(tmp_path: Path) -> None:
    """101（limit+1）**必须被拒**：422 且是配额字符串信封（解析前拦下）。"""
    client = _client(tmp_path)
    resp = _post_raw(client, "/api/sessions?launch=false", _deep_body(101))
    assert _is_quota_422(resp), f"{resp.status_code} {resp.text[:200]}"


@pytest.mark.parametrize("depth", [1500, 3000])
def test_far_over_depth_is_422_not_400(tmp_path: Path, depth: int) -> None:
    """1500/3000 必须 **422**（不是 400、不是 500）——本票与「裸 400」的核心区别。"""
    client = _client(tmp_path)
    resp = _post_raw(client, "/api/sessions?launch=false", _deep_body(depth))
    assert resp.status_code == 422, f"depth={depth} {resp.status_code} {resp.text[:200]}"
    assert _is_quota_422(resp), f"depth={depth} {resp.text[:200]}"


# ── 2. 守卫必须在解析前生效 ─────────────────────────────────────────────


def test_depth_guard_runs_before_json_parse(tmp_path: Path) -> None:
    """非法/非 JSON 且超深的 body 仍是配额 422 —— 证明守卫先于 ``request.json()``。

    判据：``b"[" * 3000 + b"not-json"`` 无法被 ``json.loads`` 解析。**解析后**的
    守卫（或任何依赖解析结果的判定）对不可解析输入结构上不可能产出配额标记——
    当前树走的正是 ``json.loads`` 的 ``RecursionError`` → ``routing.py`` 裸
    **400** ``There was an error parsing the body``。拿到固定字符串 422 即证明
    深度判定在解析之前独立完成。
    """
    client = _client(tmp_path)
    resp = _post_raw(client, "/api/sessions?launch=false", b"[" * 3000 + b"not-json")
    assert resp.status_code == 422, f"{resp.status_code} {resp.text[:200]}"
    assert _is_quota_422(resp), resp.text[:200]


# ── 3. 防过度拒绝 + 无副作用 ────────────────────────────────────────────


def test_legal_max_nesting_depth_request_still_ok(tmp_path: Path) -> None:
    """合法最大嵌套（depth=4）必须照常 200 —— 上限 100 不得伤到既有请求。"""
    client = _client(tmp_path)
    resp = client.post(
        "/api/sessions?launch=false", json=_LEGAL_MAX_DEPTH_BODY, headers=JSON_HDR
    )
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:200]}"
    assert "session_id" in resp.json()


def test_depth_rejection_has_no_side_effects(tmp_path: Path) -> None:
    """深 body 被拒 ⇒ 不得建会话目录 / 不得落任何事件（入口拒绝且无副作用）。"""
    client = _client(tmp_path)
    before = _session_dirs(tmp_path)
    for depth in (101, 1500, 3000):
        resp = _post_raw(client, "/api/sessions?launch=false", _deep_body(depth))
        assert _is_quota_422(resp), f"depth={depth} {resp.text[:200]}"
    assert _session_dirs(tmp_path) == before


# ── 4. reason 长度：字符口径，2000 通过 / 2001 拒绝 ─────────────────────


def _approve(client: TestClient, reason: str):
    # approval_id=None 是既有 seam 入口：校验通过 ⇒ 200 received（不触会话）。
    # 故 200 = 校验放行、422 = 校验拒绝，判别干净，无需起真交互式审批。
    return client.post(
        "/api/sessions/irrelevant/approve",
        json={"approval_id": None, "approved": True, "reason": reason},
    )


@pytest.mark.parametrize("size", [1999, 2000])
def test_reason_at_or_below_limit_accepted(tmp_path: Path, size: int) -> None:
    """1999/2000 字符必须放行（200 received）。"""
    client = _client(tmp_path)
    resp = _approve(client, "R" * size)
    assert resp.status_code == 200, f"size={size} {resp.status_code} {resp.text[:200]}"
    assert resp.json()["status"] == "received"


def test_reason_over_limit_is_422(tmp_path: Path) -> None:
    """2001 字符 ⇒ 422。"""
    client = _client(tmp_path)
    resp = _approve(client, "R" * 2001)
    assert resp.status_code == 422, f"{resp.status_code} {resp.text[:200]}"
    assert resp.json().get("detail")


def test_reason_limit_is_chars_not_utf8_bytes(tmp_path: Path) -> None:
    """上限按**字符**计：``"测"*2000`` = 6000 UTF-8 字节，必须放行；2001 字符 ⇒ 422。

    若误按字节计，6000 > 2000 会把合法中文直接拒掉——这条钉住口径。
    """
    client = _client(tmp_path)

    ok = _approve(client, "测" * 2000)
    assert ok.status_code == 200, f"2000 个中文（6000 字节）不得被拒：{ok.text[:200]}"

    bad = _approve(client, "测" * 2001)
    assert bad.status_code == 422, f"{bad.status_code} {bad.text[:200]}"


def test_reason_422_does_not_echo_raw_input(tmp_path: Path) -> None:
    """超限 422 响应体不得回显原始输入（audit：不得回显原始秘密）。"""
    client = _client(tmp_path)
    resp = _approve(client, "SECRET-" + "R" * 2001)
    assert resp.status_code == 422, resp.text[:200]
    assert "SECRET" not in resp.text, resp.text[:200]


# ── 5. 反向锚：既有合法请求不得被新守卫误伤 ─────────────────────────────


def test_legal_existing_requests_are_not_harmed(tmp_path: Path) -> None:
    """三端点既有合法请求的行为锚（守卫/reason 上限不得把它们变红）。"""
    client = _client(tmp_path)

    created = client.post("/api/sessions?launch=false", json={})
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]

    # messages：合法短 content（launch=false 不触发 run）
    queued = client.post(
        f"/api/sessions/{sid}/messages?launch=false", json={"content": "hello"}
    )
    assert queued.status_code in (200, 202), queued.text

    # approve：既有合法短 reason（approval_id=None seam ⇒ 200）
    approved = _approve(client, "approved by test")
    assert approved.status_code == 200, approved.text
