"""#516：列表分页（limit/offset）+ 非法 limit 422（TDD 红→绿）。

AC（票面）：
- 4000 会话 ？limit=50：p95 < 1s 且返回 ≤50 行；？limit=1 是 O(1)；
- limit=0 / -1 / abc → 422；
- 100 并发完成率 100%、p95 < 1s；混合负载 chat p95 < 2s（或比值 < 5×）。

计时类 AC 不进单测（CI 上抖动会假红）——收票时用一次性脚本跑真实入口取证。
这里钉住**结构性前提**与**契约**：

1. 切片先于摘要扫描：`read_session_summary` 的调用次数 = 返回行数 ≤ limit。
   若先扫全部摘要再切片，limit=1 也得扫 4000 个文件，O(1) 在结构上不成立；
2. 归档过滤先行：已归档的行不占 limit 预算（否则翻页出现"幽灵缺口"）；
3. limit/offset 的校验交给 FastAPI Query（ge/le），不自造一套。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import USER_MESSAGE, Session
from agent_harness.web.app import AppState, create_app, session_service


def _app(tmp_path: Path):
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return create_app(settings, enable_cors=False)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(_app(tmp_path))


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=-1", "limit=abc", "limit=0&offset=2", "offset=-1"],
)
def test_invalid_limit_offset_rejected_422(client: TestClient, query: str) -> None:
    """非法分页参数 → 422（FastAPI Query ge/le 校验，不自造一套）。"""
    resp = client.get(f"/api/sessions?{query}")
    assert resp.status_code == 422, resp.text


def test_limit_accepted_at_upper_bound(client: TestClient) -> None:
    """le=500 是声明的页大小上限：limit=500 合法（200 空列表即可）。"""
    resp = client.get("/api/sessions?limit=500")
    assert resp.status_code == 200
    assert resp.json() == []


def _seed_sessions(state: AppState, count: int) -> list[str]:
    """在真 store 里造 count 个会话（每个 ≥1 事件），并给 events.jsonl 递增 mtime。"""
    store = state.store
    ids: list[str] = []
    base_epoch = 1_700_000_000.0
    for i in range(count):
        session = Session.start(store)
        run_id, _ = session.begin_run()
        session.append(USER_MESSAGE, {"content": f"m{i}"}, run_id=run_id)
        os.utime(store._events_path(session.session_id), (base_epoch + i, base_epoch + i))
        ids.append(session.session_id)
    return ids


@pytest.mark.asyncio
async def test_limit_slices_before_summary_scan(tmp_path: Path, monkeypatch) -> None:
    """limit=5 时摘要扫描只发生在被返回的行上（结构性 O(limit) 证明）。

    30 个会话只要前 5 行：若先扫全部摘要再切片，read_session_summary 会被调
    30 次——AC「limit=1 O(1)」结构上不成立。切片必须发生在读摘要之前。
    """
    state = AppState(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test")
    )
    ids = _seed_sessions(state, 30)

    service = session_service(state)
    store = state.store
    scanned: list[str] = []
    original = store.read_session_summary

    def counting_scan(session_id: str):
        scanned.append(session_id)
        return original(session_id)

    monkeypatch.setattr(store, "read_session_summary", counting_scan)

    result = await service.list_sessions(limit=5)

    # 最近活动倒序：mtime 最大（最后创建）在前 → ids[-5:] 的逆序
    assert [s.session_id for s in result] == list(reversed(ids[-5:]))
    assert len(scanned) == 5, f"limit=5 却扫了 {len(scanned)} 个摘要"


@pytest.mark.asyncio
async def test_limit_offset_windows_are_disjoint_and_ordered(tmp_path: Path) -> None:
    """翻页窗口不重不漏，页内保持最近活动倒序。"""
    state = AppState(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test")
    )
    ids = _seed_sessions(state, 10)
    service = session_service(state)

    page1 = await service.list_sessions(limit=4)
    page2 = await service.list_sessions(limit=4, offset=4)
    page3 = await service.list_sessions(limit=4, offset=8)

    collected = [s.session_id for s in page1 + page2 + page3]
    assert collected == ids[::-1], "翻页必须覆盖全部会话且保持最近活动倒序"


@pytest.mark.asyncio
async def test_archived_rows_do_not_consume_limit_budget(tmp_path: Path) -> None:
    """归档过滤先于切片：已归档的行不占 limit 预算。

    10 个会话归档 3 个后 limit=7 应返回 7 行未归档会话——若先切片后过滤，
    只能拿到 4 行，翻页会出现越翻越少的"幽灵缺口"。
    """
    state = AppState(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test")
    )
    ids = _seed_sessions(state, 10)
    service = session_service(state)
    for sid in ids[:3]:
        await service.set_archived(sid, archived=True, entry_point="api")

    result = await service.list_sessions(limit=7)

    assert len(result) == 7
    assert all(s.session_id not in ids[:3] for s in result)
    assert all(s.archived is False for s in result)


@pytest.mark.asyncio
async def test_limit_none_keeps_full_list_semantics(tmp_path: Path) -> None:
    """不传 limit 保持全量语义（既有调用方零迁移）。"""
    state = AppState(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test")
    )
    _seed_sessions(state, 12)
    service = session_service(state)

    result = await service.list_sessions()
    assert len(result) == 12
