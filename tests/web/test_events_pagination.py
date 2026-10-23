"""#1526：REST /events 有界分页——长历史经多次读取完整重建。

票面：`GET /api/sessions/{id}/events` 原先无分页全量序列化（历史夹具读数
19800 事件 / 8.36MB 单响应）；WS/SSE 的 backlog 截断
（`STREAM_REPLAY_MAX_EVENTS` = 1000）要求客户端经 REST 补全历史，全量整包
与之相悖（部署交付层会把整个响应攒到流结束才下发）。

验收对照：
1. 有界多次读取完整重建，seq 无遗漏 / 无重复；
2. 边界（页长恰等于 limit 的追加空页）、并发新增（页间追加进后续页）有反控；
3. 404 / 409 / 422 语义不随分页参数漂移；无参数 = 既有全量（API 兼容）；
4. 同夹具响应字节读数记录（断言只钉「单页 < 全量」方向，不外推绝对值）。
"""

from __future__ import annotations

import time
import tracemalloc
from pathlib import Path

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import USER_MESSAGE, Session, SessionEvent
from agent_harness.web.app import STREAM_REPLAY_MAX_EVENTS, create_app

#: 夹具规模：> 两页（页大小 500）⇒ 多页重建被强制；不追历史 19800 的绝对值
#: （票面明言不外推），只构造「跨页」形状。
TOTAL = 1_200
PAGE = 500


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return TestClient(create_app(settings, enable_cors=False))


def _seed(client: TestClient, total: int = TOTAL) -> str:
    """建一个 `total` 条事件的会话（seq 0=session/started，1..total-1 直写 store）。

    直写 store 而非 `Session.append`：夹具只关心 REST 读路径，绕开逐条词汇
    校验/闸门，把 1200 次 fsync 的成本留在最短路径上。
    """
    session = Session.start(client.app.state.agent.store)
    store = client.app.state.agent.store
    for i in range(1, total):
        store.append_event(
            session.session_id,
            SessionEvent(
                seq=i,
                type=USER_MESSAGE,
                session_id=session.session_id,
                data={"content": f"m{i}"},
            ),
        )
    return session.session_id


def _page(client: TestClient, sid: str, after_seq: int, limit: int) -> list[dict]:
    resp = client.get(
        f"/api/sessions/{sid}/events", params={"after_seq": after_seq, "limit": limit}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_paged_rebuild_equals_full_history(tmp_path: Path) -> None:
    """多页拼接 == 无参数全量：seq 严格递增、零遗漏、零重复。"""
    client = _client(tmp_path)
    sid = _seed(client)

    full_resp = client.get(f"/api/sessions/{sid}/events")
    assert full_resp.status_code == 200, full_resp.text
    full = full_resp.json()
    assert len(full) == TOTAL

    rebuilt: list[dict] = []
    after_seq = -1
    pages = 0
    while True:
        page = _page(client, sid, after_seq, PAGE)
        pages += 1
        seqs = [e["seq"] for e in page]
        assert seqs == sorted(seqs), f"页内 seq 非递增：{seqs[:5]}…"
        if rebuilt:
            assert seqs[0] > rebuilt[-1]["seq"], "跨页 seq 回退（重复或乱序）"
        rebuilt.extend(page)
        if len(page) < PAGE:
            break
        # 游标前进守卫（C3）：后端忽略 after_seq/limit 时页永不缩短，这里会让
        # 测试秒级失败，而不是 while True 无限空转烧 CPU。
        assert seqs[-1] > after_seq, f"游标未前进（after_seq={after_seq}）"
        after_seq = seqs[-1]
        assert pages <= TOTAL, "页数超过事件总数：后端分页语义已坏"

    assert pages >= 3, f"夹具前提失效：{TOTAL} 事件 @ limit={PAGE} 应跨多页"
    assert [e["seq"] for e in rebuilt] == [e["seq"] for e in full]
    assert rebuilt == full, "分页重建与全量逐字节不一致"
    assert len({e["seq"] for e in rebuilt}) == TOTAL, "重复 seq"


def test_exact_multiple_boundary_needs_one_empty_page(tmp_path: Path) -> None:
    """历史长度恰为 limit 的整数倍：满页后必须再发一次请求拿到空页才算完结。"""
    client = _client(tmp_path)
    sid = _seed(client, total=PAGE)  # 500 条 == 一整页

    page1 = _page(client, sid, -1, PAGE)
    assert len(page1) == PAGE, "夹具前提失效：应为恰好一满页"
    page2 = _page(client, sid, page1[-1]["seq"], PAGE)
    assert page2 == [], f"边界后应得空页（非 404、非残页）：{page2}"


def test_append_between_pages_lands_in_next_page(tmp_path: Path) -> None:
    """页间并发新增：新事件进后续页，无遗漏/重复（游标 = 事实，不是快照）。"""
    client = _client(tmp_path)
    sid = _seed(client, total=11)

    page1 = _page(client, sid, -1, 5)
    assert [e["seq"] for e in page1] == [0, 1, 2, 3, 4]
    # 页间追加：绕过 Session 直写（REST 读路径对写者来源无感知）。
    client.app.state.agent.store.append_event(
        sid,
        SessionEvent(
            seq=11,
            type=USER_MESSAGE,
            session_id=sid,
            data={"content": "late"},
        ),
    )
    rebuilt = list(page1)
    after_seq = page1[-1]["seq"]
    while True:
        page = _page(client, sid, after_seq, 5)
        rebuilt.extend(page)
        if len(page) < 5:
            break
        after_seq = page[-1]["seq"]

    seqs = [e["seq"] for e in rebuilt]
    assert seqs == list(range(12)), f"并发新增后重建不完整：{seqs}"
    assert len(set(seqs)) == 12


def test_window_params_do_not_change_error_semantics(tmp_path: Path) -> None:
    """404（不存在）/ 409（损坏）/ 422（参数非法）不随分页漂移；切空窗口 = []。"""
    client = _client(tmp_path)

    missing = client.get(
        "/api/sessions/no-such-session/events", params={"after_seq": 0}
    )
    assert missing.status_code == 404, missing.text

    sid = _seed(client, total=5)
    beyond = client.get(f"/api/sessions/{sid}/events", params={"after_seq": 10**6})
    assert beyond.status_code == 200, beyond.text
    assert beyond.json() == []

    # 完整坏行（#565 语义：integrity 不健康 → 409）。注意必须带换行——不带
    # 换行的末尾残片按崩溃预期容忍（partial-tail，DEBUG 不计损坏），那是既有
    # 容错语义，不是本票要改的东西。
    events_path = client.app.state.agent.store._root / sid / "events.jsonl"
    events_path.write_text(
        events_path.read_text(encoding="utf-8") + '{"broken": \n',
        encoding="utf-8",
    )
    corrupt = client.get(f"/api/sessions/{sid}/events", params={"after_seq": 0})
    assert corrupt.status_code == 409, corrupt.text

    for params in (
        {"limit": 0},
        {"limit": STREAM_REPLAY_MAX_EVENTS + 1},
        {"after_seq": -2},
    ):
        resp = client.get(f"/api/sessions/{sid}/events", params=params)
        assert resp.status_code == 422, f"{params} 应 422：{resp.text}"


def test_param_shapes_and_compatibility(tmp_path: Path) -> None:
    """只给 limit=从头首页；只给 after_seq=默认页大小；无参数=既有全量。"""
    client = _client(tmp_path)
    sid = _seed(client, total=7)

    head = client.get(f"/api/sessions/{sid}/events", params={"limit": 3})
    assert [e["seq"] for e in head.json()] == [0, 1, 2]

    tail = client.get(f"/api/sessions/{sid}/events", params={"after_seq": 4})
    assert [e["seq"] for e in tail.json()] == [5, 6]

    legacy = client.get(f"/api/sessions/{sid}/events")
    assert len(legacy.json()) == 7, "无参数必须保持全量（既有调用方零迁移）"
    assert isinstance(legacy.json(), list), "响应形状必须仍是裸数组"


def test_response_bytes_bounded_per_page(tmp_path: Path) -> None:
    """同夹具读数（票面 AC3）：单页响应字节显著小于全量；内存（tracemalloc 峰值）
    与墙钟记录不设阈值——票面明言读数是验收证据，不外推到全部部署。"""
    client = _client(tmp_path)
    sid = _seed(client)

    tracemalloc.start()
    t0 = time.perf_counter()
    full = client.get(f"/api/sessions/{sid}/events")
    full_ms = (time.perf_counter() - t0) * 1000
    _, full_peak = tracemalloc.get_traced_memory()
    assert full.status_code == 200

    tracemalloc.reset_peak()
    page_bytes: list[int] = []
    t0 = time.perf_counter()
    after_seq = -1
    total = 0
    requests = 0
    while True:
        resp = client.get(
            f"/api/sessions/{sid}/events",
            params={"after_seq": after_seq, "limit": PAGE},
        )
        assert resp.status_code == 200
        page_bytes.append(len(resp.content))
        body = resp.json()
        total += len(body)
        requests += 1
        if len(body) < PAGE:
            break
        # 游标前进守卫（C3）：同上——语义坏掉时秒级失败，不无限空转。
        assert body[-1]["seq"] > after_seq, f"游标未前进（after_seq={after_seq}）"
        after_seq = body[-1]["seq"]
        assert requests <= TOTAL, "页数超过事件总数：后端分页语义已坏"
    paged_ms = (time.perf_counter() - t0) * 1000
    _, paged_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert total == TOTAL
    assert max(page_bytes) < len(full.content), (
        f"单页字节 {max(page_bytes)} 应小于全量 {len(full.content)}"
    )
    # 读数记录（验收证据，不设断言阈值——票面明言不外推到全部部署）。
    print(
        f"\n[#1526 读数] events={TOTAL} full={len(full.content)}B/{full_ms:.1f}ms/"
        f"peak={full_peak / 1024:.0f}KiB "
        f"pages={requests} max_page={max(page_bytes)}B "
        f"paged_total={paged_ms:.1f}ms/peak={paged_peak / 1024:.0f}KiB"
    )
