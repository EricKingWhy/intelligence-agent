#!/usr/bin/env python
"""B7 (#275) 站点量测：事件循环上的「最长单次同步占用」。

## 它回答的问题

`agent_harness` 是 async-first（不变量 #1），但有些**同步**的重活直接写在 async 函数体里。
它们在 `await` 之前是**一整块**占用——那段时间里事件循环推不动任何别的协程。本脚本只量
这一件事：**最长单次同步占用**（PERF_BASELINE §2.3 原话：「量事件循环上的最长单次同步
占用，**不是平均值**」）。

⇒ 因此**判定用 max（最长单次），中位数只作上下文**。这是票面口径，不是本脚本的选择。

## 站点（与 #275 票面一一对应）

| 站点 | 代码位置 | 同步块 |
|---|---|---|
| S1 `ws_snapshot` | `web/websocket.py:152-159` | `[e.to_dict() for e in window]` 紧接 `json.dumps(payload, default=str)` |
| S2 `artifact_save` | `storage/local_artifact.py:84-105` | 整个 `save()` 的同步体 |
| S3 `artifact_load` | `storage/local_artifact.py:110-148` | 整个 `load()` 的同步体 |

`websocket.py` 里那两段之间**没有** `await`：`_push_snapshot` 先构造 dict 字面量（含列表
推导，全同步），再把 dict 交给 `_send_json`，而 `_send_json` 里 `json.dumps(payload,
default=str)` 的实参求值也发生在 `await websocket.send_text(...)` 之前 ⇒ 两段合成**一块**，
分开量会低估。

S2 / S3 的关键事实：`save` / `load` 是 `async def`，但**函数体里一个 `await` 都没有**
⇒ 调用方 `await store.save(...)` 拿到控制权后一口气跑完，中途不让出。所以「最长单次同步
占用」**就是**整个函数的墙钟。本脚本用 `_has_await()` 在运行时把这条前提核一遍
（不靠读代码断言）。

## 判定规则（票面第 1 步，二值）

某站点在**真实上界**下的**最长单次**占用 **< 5ms** ⇒ 判定 **无需搬线程**（并留数字）；
**≥ 5ms** ⇒ **必须搬**。

## 「真实上界」的来源（是推出来的，不是估的）

- **S1**：`STREAM_REPLAY_MAX_EVENTS = 1000`（`web/app.py:629`）⇒ 窗口上界 = **1000 事件**。
  注意上界卡的是**条数**不是字节：单条事件能有多大取决于事件类型（见下）。脚本对每种字节
  实现都测，**判定取最坏的那种**（契约不限制内容的语言/大小，取最好看的那种等于自设上界）。
  1. **真实生产会话**里字节数最大 / 中位 / 最小 的三个 1000 事件窗口（滑窗，非随便截一段）；
  2. **契约尾窗**：1000 × 8KB 载荷（`TOOL_OUTPUT_MAX_FRAME_CHARS`，流式输出单帧上限）。
- **S2 / S3**：内容的真实上界 = **写入者契约上界**。生产里唯一写入者是 `tooling/overflow.py`：
  它把超过 `artifact_overflow_chars`（默认 2000）的**工具结果字段原样**交给 store
  （`overflow.py:88-94`）。字段本身的上界是沙箱捕获上限
  `LocalSandbox(max_capture_chars=2_000_000)`（`sandbox/local.py:121`），
  `tools/bash.py:124-132` 把 `result.stdout` **原样**放进 `ToolResult.data`。
  ⇒ **2,000,000 字符** = S2 的真实上界。但上界卡的是**字符**不是字节 ⇒ 同一句内容
  可以是 2MB（ASCII）也可以是 6MB（CJK，UTF-8 3 字节/字符）。**两种实现都测，判定取坏的那种**
  （本项目是中文优先：`read` 一篇中文文档、`git log` 的中文提交信息都是常见形态）。

## 控制列（必须看）

每轮同时跑一条**与站点无关的固定工作量**（纯 Python 定长循环），与站点数字同屏。
理由是 F4 的实测教训：单次采样会被机器状态（JIT / GC / 后台负载）主导，**没有控制列就
看不出漂移**，会把自己的抖动当成改造收益。

## GC 双跑（机制诊断，不是判定）

「最长单次」的尾巴常常是 GC 停顿，所以脚本对 S1 再做一次 `gc on/off` 的**块级交替**
A/B（**不是**跑两整轮——实测过一次「整轮跑两遍」之间控制列从 12.06 漂到 42.12 ms，
3.5×，那种差值量的是机器不是 GC）。块级交替把漂移摊到两臂，且**每块都带控制列**：
块内控制列一致（漂移 < 5%）才认这两臂可比，报告里会打印这个数。
**判定只取 gc=normal 那一轮**（那才是生产形态）。

⚠ 由这条诊断要注意一个反直觉的结论：把重活搬进 `to_thread` **消不掉 GC 造成的尾巴**
——CPython 的 GC 在收集期间持有 GIL，无论哪个线程触发停顿，事件循环同样停。搬线程
消掉的是**非 GC 的那部分 CPU**（`json.dumps` / `sha256` / `encode` / 真正让出的阻塞 IO）。

## 用法

    python scripts/measure_loop_blocking.py
    python scripts/measure_loop_blocking.py --repeats 50 --json out.json
    python scripts/measure_loop_blocking.py --sessions-root D:/x/.agent/workspace/sessions

退出码：0 = 全部站点判定「无需搬线程」；1 = 有站点 ≥ 5ms（票面要求搬）；2 = 夹具不足。
"""

from __future__ import annotations

import argparse
import asyncio
import dis
import gc
import json
import math
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import anyio

#: 每个站点取样次数。取 max 判定 ⇒ 样本要多，否则 max 是运气不是上界。
REPEATS = 50
WARMUP = 5

#: 票面判定阈值（ms）。二值：最长单次 < 阈值 不搬；>= 阈值 必须搬。
THRESHOLD_MS = 5.0

#: ws 快照窗口上界，与 `web/app.py` 的 STREAM_REPLAY_MAX_EVENTS 对齐。不 import
#: （那会拉起整条 web.app 依赖链）；改在源码里做字面量核对，常量变了会当场报 ❌
#: 而不是静默用错上界。
STREAM_WINDOW = 1000

#: 沙箱单通道捕获上限（`sandbox/local.py:121` 的 max_capture_chars 默认值）。
SANDBOX_CAPTURE_CHARS = 2_000_000

#: 流式输出单帧字符上限（`tooling/output_stream.py:26`）。
TOOL_OUTPUT_MAX_FRAME_CHARS = 8_192

_SESSIONS_ROOT_DEFAULT = Path(".agent/workspace/sessions")

#: 控制列：与任何站点无关的定长纯 Python 工作量。
_CONTROL_N = 200_000


# —— 计时 ——


def _percentile(sorted_ms: list[float], q: float) -> float:
    """最近秩法（`ceil(q·n)-1`）。⚠ 不要写成 `int(q*n)`：n=15 时 p95 会退化成 max，
    让「p95 与 max 相同」看起来像 bug 而不像巧合。"""
    if not sorted_ms:
        return float("nan")
    return sorted_ms[max(0, math.ceil(q * len(sorted_ms)) - 1)]


def _stats(samples: list[float]) -> dict[str, float]:
    samples.sort()
    return {
        "max_ms": samples[-1],
        "median_ms": statistics.median(samples),
        "p90_ms": _percentile(samples, 0.90),
        "min_ms": samples[0],
        "n": len(samples),
    }


def bench(fn: Callable[[], object], *, repeats: int = REPEATS) -> dict[str, float]:
    """同步块的墙钟（ms）：预热 + repeats 次取样。"""
    for _ in range(WARMUP):
        fn()
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return _stats(samples)


async def abench(fn: Callable[[], object], *, repeats: int = REPEATS) -> dict[str, float]:
    """async 版本：量 `await fn()` 的整段墙钟。

    仅当 `fn` 的协程体内**零 await** 时，这个数才等于「最长单次同步占用」——
    前提由 `_has_await()` 运行时核（见 `measure_artifact`）。
    """
    for _ in range(WARMUP):
        await fn()
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        await fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return _stats(samples)


def control_block() -> None:
    acc = 0
    for i in range(_CONTROL_N):
        acc = (acc + i * i) % 1_000_003
    if acc < 0:  # pragma: no cover — 让解释器无法把循环整个优化掉
        raise AssertionError


def bench_ab_gc(fn: Callable[[], object], *, blocks: int = 6,
                per_block: int = 8) -> dict:
    """gc on / off 的**块级交替** A/B（机制诊断，不作为判定依据）。

    为什么是块级交替而不是「跑两整轮」：整轮跑两遍时，机器状态在两轮之间
    可能整体改变（本机实测过一次控制列 12.06 → 42.12 ms，3.5×），那时两轮
    的差值量的是机器而不是 GC。块级交替把漂移摊到两臂上，并且**每块都带一条
    控制列**——块间控制列一致才说明这两臂可比较。

    为什么块内 ≥6 次取样而不是逐次交替：`gc.enable()` 之后不立即回收，
    A 臂的垃圾会留给 B 臂下一次取样去收，逐次交替会把这个结转读成"B 臂更慢"。
    """
    for _ in range(WARMUP):
        fn()
    arms: dict[str, list[float]] = {"gc_on": [], "gc_off": []}
    controls: list[tuple[float, float]] = []
    for _ in range(blocks):
        for arm in ("gc_on", "gc_off"):
            before = bench(control_block, repeats=3)["median_ms"]
            if arm == "gc_off":
                gc.disable()
            try:
                for _ in range(per_block):
                    start = time.perf_counter()
                    fn()
                    arms[arm].append((time.perf_counter() - start) * 1000.0)
            finally:
                if arm == "gc_off":
                    gc.enable()
            after = bench(control_block, repeats=3)["median_ms"]
            controls.append((before, after))
    drift = [abs(a - b) / a for a, b in controls]
    return {
        "gc_on": _stats(arms["gc_on"]),
        "gc_off": _stats(arms["gc_off"]),
        "blocks": blocks,
        "per_block": per_block,
        # 块内控制列的两端差：同一块里控制列不该变（放大 100 倍看百分比）
        "control_drift_pct_max": max(drift) * 100.0,
        "comparable": max(drift) < 0.05,
    }


# —— 前提核对（只读，不改任何东西）——


def _check_literal(source: Path, name: str, expected: str) -> str:
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as error:
        return f"⚠ 读不到 {source}: {error}"
    if name in text and expected in text:
        return f"✅ {source.name}: `{name}` … `{expected}` 均在文中"
    return (f"❌ {source.name} 里找不到 `{name}` 或 `{expected}`——常量可能已变，"
            "本脚本的上界推理需要重做")


def _has_await(fn: Callable[..., object]) -> bool:
    """这个（协）函数体内有没有 `await`。

    用 `GET_AWAITABLE` 操作码探测：`inspect.CO_AWAIT` 在现代 CPython 里**不存在**
    （实测 AttributeError）；`CO_COROUTINE` 只说明"是协程"，不说明里面有 await。
    """
    code = getattr(fn, "__code__", None)
    return _scan_ops(code) if code is not None else False


def _scan_ops(code) -> bool:
    if any(i.opname in {"GET_AWAITABLE", "GET_AITER", "GET_ANEXT"}
           for i in dis.get_instructions(code)):
        return True
    return any(_scan_ops(const) for const in code.co_consts
               if hasattr(const, "co_code"))


# —— 夹具 ——


def _serialized_sizes(events: list) -> list[int]:
    # 与 `_send_json` 逐字同参：default=str、ensure_ascii 取默认 True。
    # ⚠ 这一条重要：磁盘上的 JSONL 是 ensure_ascii=False，WS 帧是 True，
    #   中文被转义成 \uXXXX ⇒ 帧比磁盘上的行大得多。
    return [len(json.dumps(e.to_dict(), default=str)) for e in events]


def _window_at(events: list, sizes: list[int], want: int, offset: int) -> tuple[list, int]:
    return events[offset:offset + want], sum(sizes[offset:offset + want])


def _real_windows(sessions_root: Path, want: int) -> list[tuple[str, list, dict]]:
    """真实会话里按字节数取的 [最大 / 中位 / 最小] 三个窗口。

    取三个而不是只取最大的一个：只报最大窗口等于替票面选了最坏场景，
    只报一个窗口也看不出窗口之间的波动。
    """
    from agent_harness.session.event import SessionEvent

    if not sessions_root.is_dir():
        return []
    paths = sorted(
        (d / "events.jsonl" for d in sessions_root.iterdir()
         if (d / "events.jsonl").is_file()),
        key=lambda p: p.stat().st_size, reverse=True,
    )[:5]
    out: list[tuple[str, list, dict]] = []
    for path in paths:
        events = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(SessionEvent.from_dict(json.loads(line)))
                except Exception:  # noqa: BLE001,S112 — 坏行跳过是刻意的：夹具不该
                    continue       # 因为一行脏数据整体失败（真实会话里确实见过）
        if len(events) < want:
            continue
        sizes = _serialized_sizes(events)
        ranked = sorted(range(len(sizes) - want + 1),
                        key=lambda i: sum(sizes[i:i + want]))
        picks = {"最大": ranked[-1], "中位": ranked[len(ranked) // 2],
                 "最小": ranked[0]}
        session_sizes = sorted(sizes)
        for tag, offset in picks.items():
            window, window_bytes = _window_at(events, sizes, want, offset)
            out.append((f"真实会话 {path.parent.name[:8]}… / {tag}窗口", window, {
                "session_id": path.parent.name,
                "session_events": len(events),
                "window_events": want,
                "window_bytes": window_bytes,
                "window_offset": offset,
                "event_bytes_p50": session_sizes[len(session_sizes) // 2],
                "event_bytes_p99": session_sizes[
                    min(len(session_sizes) - 1, int(len(session_sizes) * 0.99))],
                "event_bytes_max": session_sizes[-1],
            }))
    return out


def _synthetic_window(want: int, payload_chars: int) -> tuple[list, dict]:
    from agent_harness.session.event import SessionEvent

    blob = "x" * payload_chars
    events = [
        SessionEvent(
            event_id=f"syn-{i:06d}", seq=i, time="2026-09-18T00:00:00.000+00:00",
            type="tool/output", session_id="b7-synthetic", run_id="r1",
            step_id=1, block_id="b1", data={"output": blob},
        )
        for i in range(want)
    ]
    return events, {
        "session_id": "(synthetic)",
        "window_events": want,
        "window_bytes": sum(_serialized_sizes(events)),
        "payload_chars_per_event": payload_chars,
    }


# —— 站点测量 ——


def measure_ws_snapshot(window: list, repeats: int) -> dict[str, dict[str, float]]:
    """S1：`websocket.py:152-159` 那一整块（列表推导 + dict + json.dumps）。"""
    prebuilt = {
        "type": "snapshot", "session_id": "b7-measure", "replay_upto": 0,
        "has_active_run": True, "events": [e.to_dict() for e in window],
    }
    base = {k: v for k, v in prebuilt.items() if k != "events"}

    def combined():
        payload = {**base, "events": [e.to_dict() for e in window]}
        return json.dumps(payload, default=str)

    return {
        "combined (真实占用块)": bench(combined, repeats=repeats),
        "  ├ to_dict × N": bench(lambda: [e.to_dict() for e in window], repeats=repeats),
        "  └ json.dumps": bench(lambda: json.dumps(prebuilt, default=str),
                                repeats=repeats),
    }


def measure_ws_single_frame(event, repeats: int) -> dict[str, dict[str, float]]:
    """S1'：`_write_loop` → `_send_json` 的单条事件帧（同样两步无 await 间隔）。"""
    prebuilt = {"type": "event", "session_id": "b7-measure", "event": event.to_dict()}

    def combined():
        return json.dumps({"type": "event", "session_id": "b7-measure",
                           "event": event.to_dict()}, default=str)

    return {"combined (单帧)": bench(combined, repeats=repeats),
            "  └ json.dumps": bench(lambda: json.dumps(prebuilt, default=str),
                                    repeats=repeats)}


def _write_atomic_once(store, path: Path, content: str) -> None:
    """把「`_write_atomic` 整段」单独量（save 里被调两次：内容 + 元数据）。"""
    store._write_atomic(path, content.encode("utf-8"))


async def measure_artifact(content: str, repeats: int, label: str) -> dict:
    """S2 / S3：`LocalArtifactStore.save` / `load`，并分解内部同步原语。"""
    from agent_harness.config import Settings
    from agent_harness.storage.artifact import compute_artifact_id
    from agent_harness.storage.local_artifact import LocalArtifactStore

    session_id = "b7-measure"
    with tempfile.TemporaryDirectory(prefix="wbi-b7-") as tmp:
        store = LocalArtifactStore(Settings(artifact_dir=tmp), session_id=session_id)
        has_await = (_has_await(LocalArtifactStore.save)
                     or _has_await(LocalArtifactStore.load))

        async def do_save():
            await store.save(session_id, content, mime_type="text/plain",
                             source_tool="bash", tool_call_id="tc-1")

        await do_save()  # 先写一份供 load 用
        artifact_id = compute_artifact_id(content)
        path = store._content_path(artifact_id)  # 量测脚本，读私有路径可接受

        async def do_load():
            await store.load(artifact_id)

        return {
            "save (整个函数体)": await abench(do_save, repeats=repeats),
            "load (整个函数体)": await abench(do_load, repeats=repeats),
            "  ├ encode('utf-8')": bench(lambda: content.encode("utf-8"), repeats=repeats),
            "  ├ sha256 (compute_artifact_id)": bench(
                lambda: compute_artifact_id(content), repeats=repeats),
            "  ├ _write_atomic (write+replace)": bench(
                lambda: _write_atomic_once(store, path, content), repeats=repeats),
            "  ├ read_bytes": bench(lambda: path.read_bytes(), repeats=repeats),
            "  └ read_bytes + decode": bench(
                lambda: path.read_bytes().decode("utf-8"), repeats=repeats),
            "_meta": {
                "label": label,
                "chars": len(content),
                "utf8_bytes": len(content.encode("utf-8")),
                "coroutine_has_await": has_await,
            },
        }


# —— 报告 ——


def _verdict(max_ms: float) -> str:
    return "✅ 无需搬线程" if max_ms < THRESHOLD_MS else "❌ 必须搬线程"


def _row(name: str, stats: dict[str, float]) -> str:
    return (f"  {name:<40} 最长 {stats['max_ms']:9.3f} ms   "
            f"p90 {stats['p90_ms']:8.3f}   中位 {stats['median_ms']:8.3f}")


def _write_json(path: str, report: dict) -> None:
    """把报告落盘（同步；由 `anyio.to_thread.run_sync` 调用）。"""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)


def _print_site(title: str, rows: dict, judged: str | None = None) -> None:
    print(f"\n=== {title} ===")
    for name, stats in rows.items():
        if name.startswith("_"):
            continue
        print(_row(name, stats))
        if name == judged:
            print(f"  {'':<40} ⇒ 判定：{_verdict(stats['max_ms'])}"
                  f"（阈值 {THRESHOLD_MS} ms，判据 = 最长单次）")


async def _run_pass(gc_mode: str, fixtures: dict, ladders: list,
                    repeats: int) -> tuple[dict, dict]:
    """跑一整轮（一种 gc 形态）。返回 (report, verdicts)。"""
    report: dict = {"gc_mode": gc_mode}
    verdicts: dict[str, dict] = {}
    if gc_mode == "disabled":
        gc.disable()
    try:
        report["control"] = bench(control_block, repeats=repeats)
        print(f"\n########## gc={gc_mode} ##########")
        print("=== 控制列（与本票无关的固定工作量；看机器漂移用）===")
        print(_row(f"control loop ×{_CONTROL_N}", report["control"]))

        print("\n=== 夹具 ===")
        for name, (_, meta) in fixtures.items():
            print(f"  {name}: {json.dumps(meta, ensure_ascii=False)}")

        for name, (window, meta) in fixtures.items():
            rows = measure_ws_snapshot(window, repeats)
            report[f"S1 ws_snapshot — {name}"] = {"meta": meta, **rows}
            judged = None if "不参与判定" in name else "combined (真实占用块)"
            _print_site(f"S1 ws_snapshot — {name}（窗口 {meta['window_bytes']:,} 字节）",
                        rows, judged)
            if judged:
                verdicts.setdefault(f"S1 ws_snapshot — {name}", {
                    "max_ms": rows[judged]["max_ms"],
                    "median_ms": rows[judged]["median_ms"],
                    "mark": _verdict(rows[judged]["max_ms"]),
                    "basis": "真实生产会话窗口",
                })
            frames = measure_ws_single_frame(window[0], repeats)
            report[f"S1' ws 单帧 — {name}"] = {"meta": meta, **frames}
            _print_site("S1' ws 单帧（单条事件帧）", frames)

        for chars, label, filler, judged_site in ladders:
            rows = await measure_artifact(filler * chars, repeats, label)
            report[f"S2/S3 artifact — {label}"] = rows
            meta = rows["_meta"]
            print(f"\n=== S2/S3 LocalArtifactStore — {label} ===")
            print(f"  内容 {meta['chars']:,} 字符 / {meta['utf8_bytes']:,} 字节；"
                  f"两个函数体内含 await = {meta['coroutine_has_await']}"
                  f"（False 才成立「整个函数体 = 一块同步占用」）")
            for key, stats in rows.items():
                if not key.startswith("_"):
                    print(_row(key, stats))
            if judged_site:
                for site, key in (("S2 save", "save (整个函数体)"),
                                  ("S3 load", "load (整个函数体)")):
                    mark = _verdict(rows[key]["max_ms"])
                    print(f"  {site} ⇒ 判定：{mark}（阈值 {THRESHOLD_MS} ms，"
                          "判据 = 最长单次）")
                    verdicts[f"{site} @{label}"] = {
                        "max_ms": rows[key]["max_ms"],
                        "median_ms": rows[key]["median_ms"],
                        "mark": mark, "basis": "写入者契约上界",
                    }
    finally:
        if gc_mode == "disabled":
            gc.enable()
    return report, verdicts


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sessions-root", default=str(_SESSIONS_ROOT_DEFAULT))
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument("--json", metavar="PATH")
    parser.add_argument("--allow-synthetic", action="store_true",
                        help="找不到真实会话时允许合成窗口（S1 判定将不可用）")
    parser.add_argument("--only", choices=("all", "s1", "artifact"), default="all",
                        help="只跑某类站点（重复采样时省时间；判定语义不变）")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    print("=== 前提核对：上界推理所依据的常量 ===")
    checks = [
        _check_literal(repo_root / "src/agent_harness/web/app.py",
                       "STREAM_REPLAY_MAX_EVENTS", "1000"),
        _check_literal(repo_root / "src/agent_harness/sandbox/local.py",
                       "max_capture_chars", "2_000_000"),
        _check_literal(repo_root / "src/agent_harness/tooling/output_stream.py",
                       "TOOL_OUTPUT_MAX_FRAME_CHARS", "8_192"),
    ]
    for line in checks:
        print("  " + line)

    real = _real_windows(Path(args.sessions_root), STREAM_WINDOW)
    synthetic = not real
    if synthetic and not args.allow_synthetic:
        print(f"\n❌ 找不到 ≥{STREAM_WINDOW} 事件的真实会话。指定 --sessions-root，或"
              " --allow-synthetic（S1 判定不可用）。")
        return 2
    fixtures: dict[str, tuple[list, dict]] = {}
    if synthetic:
        fixtures["合成窗口（--allow-synthetic；不参与判定）"] = _synthetic_window(
            STREAM_WINDOW, TOOL_OUTPUT_MAX_FRAME_CHARS)
    else:
        for name, window, meta in real:
            fixtures[name] = (window, meta)
    fixtures["契约尾窗（1000 × 8KB；不参与判定）"] = _synthetic_window(
        STREAM_WINDOW, TOOL_OUTPUT_MAX_FRAME_CHARS)

    ladders = [
        (64 * 1024, "常见级 64KiB 字符（read / MCP / skill 工具上限）", "x", False),
        (300 * 1024, "多字段级 300KiB 字符（diff before+after 的 JSON）", "x", False),
        (SANDBOX_CAPTURE_CHARS, "契约上界 2M 字符 ASCII（2MB 落盘）", "x", False),
        (SANDBOX_CAPTURE_CHARS, "契约上界 2M 字符 CJK（6MB 落盘）", "中", True),
    ]

    report: dict = {"threshold_ms": THRESHOLD_MS, "repeats": args.repeats,
                    "warmup": WARMUP, "constant_checks": checks, "passes": {}}
    run_fixtures = fixtures if args.only in ("all", "s1") else {}
    run_ladders = ladders if args.only in ("all", "artifact") else []
    normal, verdicts = await _run_pass("normal", run_fixtures, run_ladders,
                                       args.repeats)
    report["passes"]["normal"] = normal

    # —— 机制诊断：最长单次的尾巴是不是 GC（不作为判定依据）——
    report["gc_ab"] = {}
    ab_targets = [k for k in run_fixtures if "最大窗口" in k] or list(run_fixtures)
    if ab_targets:
        print("\n=== 机制诊断：gc on/off 块级交替 A/B（不作为判定依据）===")
    base = {"type": "snapshot", "session_id": "b7-measure", "replay_upto": 0,
            "has_active_run": True}
    for name in ab_targets[:1]:
        window, meta = fixtures[name]

        def combined(window=window, base=base):
            payload = {**base, "events": [e.to_dict() for e in window]}
            return json.dumps(payload, default=str)

        ab = bench_ab_gc(combined, blocks=6, per_block=8)
        report["gc_ab"][f"S1 {name}"] = ab
        print(f"  S1 {name}（窗口 {meta['window_bytes']:,} 字节）")
        for arm in ("gc_on", "gc_off"):
            s = ab[arm]
            print(f"    {arm:<7} 最长 {s['max_ms']:8.3f} ms / p90 {s['p90_ms']:8.3f}"
                  f" / 中位 {s['median_ms']:8.3f}")
        print(f"    块内控制列最大漂移 {ab['control_drift_pct_max']:.2f}%"
              f" ⇒ 两臂可比 = {ab['comparable']}")

    print(f"\n=== 判定汇总（阈值 {THRESHOLD_MS:.1f} ms；判据 = 最长单次；取 gc=normal 轮）===")
    for name, info in verdicts.items():
        print(f"  {info['mark']}  {name}")
        print(f"      最长 {info['max_ms']:.3f} ms / 中位 {info['median_ms']:.3f} ms"
              f"（{info['basis']}）")
    if synthetic:
        print("  ⚠ 夹具为合成窗口——以上 S1 判定**不可用**，仅供形态参考。")
    report["verdicts"] = verdicts
    report["synthetic_fixture"] = synthetic

    if args.json:
        # 走线程写（ASYNC230）：本脚本自己的纪律——async 里不做阻塞调用。
        await anyio.to_thread.run_sync(_write_json, args.json, report)
        print(f"\n全部数字已写入 {args.json}")

    if synthetic:
        return 2
    return 1 if any(v["mark"].startswith("❌") for v in verdicts.values()) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
