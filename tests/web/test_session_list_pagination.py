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
3. limit/offset 的校验交给 FastAPI Query（ge/le），不自造一套；
4. 并发突发共享同一次目录扫描（single-flight，用户裁决第五选项）——但**不跨请求
   缓存**（mtime 现读契约的闸门侧钉子，见文件末两条测试）。
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


# —— #516 并发 AC：扫描合并（single-flight；用户裁决第五选项，2026-10-02）——


def _burst_list(store, workers: int = 100) -> list[list[str]]:
    """真 workers 线程**同拍**调 `store.list_session_ids`。

    两个要点（缺一就在满载车道上假红）：
    - 绕开 `anyio.to_thread` 的默认 40 线程限流（否则 100 个调用分 ~3 批，闸门
      各批各扫一次，`== 1` 的机制断言不成立）；
    - `threading.Barrier` 先把 workers 个线程对齐到同一瞬间再放行——不设闸，
      机器满载时线程起飞能错开超过 leader 的持窗时长，会有迟到者自成新批。
    """
    barrier = threading.Barrier(workers)

    def aligned() -> list[str]:
        barrier.wait()
        return store.list_session_ids()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(aligned) for _ in range(workers)]
        return [f.result() for f in futures]


def test_list_sync_limiter_capacity_fits_ac_burst() -> None:
    """列表专用 to_thread 限流器容量 ≥ AC 的 100 并发（承重常数，#508 先例钉住）。

    容量不足会把突发切回多波：单飞闸门只能每波各坍缩成 1 次扫描（cap 40 实测
    4000 会话 p95 914–1131ms 贴 1s 线；cap 100 单波单扫 719–776ms）。
    """
    from agent_harness.session.service import _LIST_SYNC_LIMITER

    assert _LIST_SYNC_LIMITER.total_tokens >= 100


def test_concurrent_burst_coalesces_to_one_scan(tmp_path: Path, monkeypatch) -> None:
    """并发突发共享同一次目录扫描（AC「100 并发 p95<1s」的结构性证明）。

    墙钟不进 CI 断言（抖动假红，收票用一次性脚本对真实入口取证）；这里钉机制：
    100 线程同拍调用，leader 真扫一次、其余共享 in-flight 结果（nginx
    `proxy_cache_lock` / Go singleflight 同型）。无闸门时 scan_count == 100（红）。
    """
    state = AppState(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test")
    )
    ids = _seed_sessions(state, 20)
    store = state.store
    scans: list[int] = []
    original = store._list_session_ids_uncached

    def slow_counting_scan():
        scans.append(1)
        time.sleep(0.5)  # 撑住 in-flight 窗口（Barrier 已对齐起飞，0.5s 是满载裕度）
        return original()

    monkeypatch.setattr(store, "_list_session_ids_uncached", slow_counting_scan)

    results = _burst_list(store)

    assert len(scans) == 1, f"100 并发只应真扫 1 次，实际 {len(scans)} 次"
    expected = list(reversed(ids))
    for result in results:
        assert result == expected, "共享方必须拿到与 leader 一致的最近活动倒序"
    assert len({id(r) for r in results}) == len(results), (
        "共享不得产生别名：waiter 必须拿到副本，互不影响调用方的后续 mutation"
    )


def test_gate_does_not_cache_across_requests(tmp_path: Path, monkeypatch) -> None:
    """扫描结束即散、不跨请求缓存（mtime 现读契约的闸门侧钉子）。

    两次串行请求 = 两次真扫；中间外部 `os.utime` 抬旧会话 → 第二次立即看到新序。
    `test_list_by_workspace_follows_ledger_order_not_activity` 钉的是项目视图，
    这里钉默认列表路径。若有人日后给闸门加 TTL 缓存，本测试转红。
    """
    state = AppState(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test")
    )
    ids = _seed_sessions(state, 5)
    store = state.store
    scans: list[int] = []
    original = store._list_session_ids_uncached

    def counting_scan():
        scans.append(1)
        return original()

    monkeypatch.setattr(store, "_list_session_ids_uncached", counting_scan)

    assert store.list_session_ids() == list(reversed(ids))
    # 外部 utime：把最旧（base_epoch）的会话抬到最新
    os.utime(store._events_path(ids[0]), (2_000_000_000.0, 2_000_000_000.0))
    assert store.list_session_ids()[0] == ids[0]
    assert len(scans) == 2, "闸门不得跨请求缓存扫描结果"


def test_leader_scan_failure_propagates_and_gate_recovers(
    tmp_path: Path, monkeypatch
) -> None:
    """leader 扫描异常 → 同批 waiter 收到同一异常；闸门不残留，下一请求正常新扫。"""
    state = AppState(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test")
    )
    _seed_sessions(state, 5)
    store = state.store
    scans: list[int] = []
    original = store._list_session_ids_uncached

    def failing_scan():
        scans.append(1)
        time.sleep(0.5)
        raise OSError("simulated disk failure")

    monkeypatch.setattr(store, "_list_session_ids_uncached", failing_scan)

    outcomes: list[str] = []
    with ThreadPoolExecutor(max_workers=100) as pool:
        barrier = threading.Barrier(100)

        def aligned() -> list[str]:
            barrier.wait()
            return store.list_session_ids()

        futures = [pool.submit(aligned) for _ in range(100)]
        for future in futures:
            try:
                future.result()
                outcomes.append("ok")
            except OSError:
                outcomes.append("OSError")

    assert len(scans) == 1, f"失败的一批也只应真扫 1 次，实际 {len(scans)} 次"
    assert outcomes == ["OSError"] * 100, "同批 waiter 必须收到 leader 的异常"

    # 闸门不残留：恢复真扫描后下一请求成功（失败不再重放）——重新包一层计数
    # 包装，让「走了新扫」本身被计数证实而非仅靠未抛错（独立审查 F5）。
    recovered_scans: list[int] = []

    def counting_recovered():
        recovered_scans.append(1)
        return original()

    monkeypatch.setattr(store, "_list_session_ids_uncached", counting_recovered)
    recovered = store.list_session_ids()
    assert isinstance(recovered, list) and recovered
    assert len(recovered_scans) == 1, "恢复后的请求应走一次新扫描而非重放失败批"
    assert len(scans) == 1
