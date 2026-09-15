#!/usr/bin/env python
"""交付层 SSE 流式量测：响应头何时到、帧何时到、keepalive 有没有真的上线。

## 它回答的问题

部署交付层（`Server: CloudStudio Gateway`，前置腾讯 EdgeOne）会把**整个 HTTP 响应**
攒到流结束才下发。用户可见症状：前端在 run 跑完之前拿不到任何字节，答案"唰"地
一次性出现，打字机效果不存在。

本脚本用**原始 SSE 通道**（不经过前端、不经过 WebSocket）量三件事，从而把
"是后端没发 / 是中间层攒住 / 是被压缩"三者分开：

1. `t_headers`：响应头何时到达（攒包的第一个指纹——本地直连是毫秒级）；
2. 帧到达时间线：区分**注释帧**（`: ping`，keepalive）与**数据帧**（`data:`），
   并打印前若干条的到达时刻；
3. 响应头真相：`transfer-encoding` / `content-encoding` / `x-accel-buffering`
   ——中间层若把 chunked 改写成带 `Content-Length` 的完整响应、或加了
   `content-encoding: gzip`，那就不是"配置没调"而是它在按整包处理。

## 判定规则（脚本会自己给结论）

- 帧**阶梯式**到达、且 keepalive 注释帧周期性出现 → 交付层放行字节，流式正常；
- `t_headers` ≈ run 全长、数据帧全挤在最后 → 交付层攒包（keepalive 无效）；
- 完全看不到注释帧 → 该部署没开 keepalive / 间隔比 run 还长（库默认 15s）/
  中间层吞了注释帧。**这一条不能单独用来判断"能不能流式"**：攒包的判据是
  响应头到达时刻——响应头也被拖到流末就说明整包被攒，此时注释帧多密都没用。

## 用法

    # 本地（无中间层）：先起服务，再量
    python scripts/measure_sse_streaming.py --base-url http://127.0.0.1:8010

    # 真实交付链路：同一把尺子量一次，两边对比
    python scripts/measure_sse_streaming.py --base-url https://<交付层域名>

    # 量"刷新后接回流"那条通道（需要一个已存在的、run 在途的会话）
    python scripts/measure_sse_streaming.py --base-url <url> --mode resume --session-id <sid>

`--mode create` 走 `POST /api/sessions`（Composer 提交新任务的通道）；
`--mode resume` 走 `GET /api/sessions/{sid}/stream`（刷新后的接流通道）。
两条通道都受同一个交付层影响，但代码路径不同，可能各自表现不同。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

#: 默认任务：要求一段较长的连续输出。**必须够长**——量测的是"字节在 run 期间
#: 是否持续流动"，一个 2 秒就答完的任务在任何部署上都看不出差别。
DEFAULT_TASK = (
    "请写一篇大约 600 字的中文短文，主题：为什么事件溯源适合 Agent 运行时。"
    "只输出正文，不要调用任何工具，不要分点。"
)


class Timeline:
    """帧到达时间线：把原始字节切成 SSE 帧并按到达时刻记账。"""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.headers_at: float | None = None
        self.frames: list[dict] = []
        self._buf = b""

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            line = raw.strip()
            if not line:
                continue
            text = line.decode("utf-8", "replace")
            # 存**整行**（JSON 产物要能当取证材料用：截断过会把 session_id 这类
            # 尾部字段切掉，回头想拿它清理探针会话都拿不到）；截断只发生在打印。
            self.frames.append({
                "t": round(self.elapsed(), 3),
                "kind": "comment" if text.startswith(":") else "data",
                "line": text,
            })

    # ── 汇总 ──

    @property
    def comments(self) -> list[dict]:
        return [f for f in self.frames if f["kind"] == "comment"]

    @property
    def data_frames(self) -> list[dict]:
        return [f for f in self.frames if f["kind"] == "data"]

    def first_data_at(self) -> float | None:
        data = self.data_frames
        return data[0]["t"] if data else None

    def last_at(self) -> float | None:
        return self.frames[-1]["t"] if self.frames else None

    def verdict(self) -> tuple[str, str]:
        return compute_verdict(self.frames, self.headers_at)


def compute_verdict(
    frames: list[dict], headers_at: float | None,
) -> tuple[str, str]:
    """(结论标记, 说明)。**纯函数**：只依据观测到的事实，不猜中间层实现。

    独立成函数是为了能对着合成时间线测「攒包」那一分支——一个只会说"OK"的
    诊断工具比没有更糟：它会把真实故障粉饰成通过。
    """
    if not frames:
        return "❌ 无字节", "流结束时一个帧都没收到——请求本身可能失败。"
    comments = [f for f in frames if f["kind"] == "comment"]
    data = [f for f in frames if f["kind"] == "data"]
    first_data = data[0]["t"] if data else None
    last = frames[-1]["t"]
    if not comments:
        return (
            "⚠ 无 keepalive",
            (
                "整条流里没有任何 `: ping` 注释帧。三种可能：① 该部署没开 keepalive；"
                "② keepalive 间隔比这次 run 还长（sse-starlette 库默认 15s，"
                "短于 15s 的 run 一个 ping 都不会产生）；③ 中间层把注释帧吞了。"
                "注意这一条**单独不足以**判断能不能流式——真正的判据是响应头何时到"
                "（若响应头也被拖到流末，说明整包被攒，注释帧多密都没用）。"
            ),
        )
    headers = headers_at if headers_at is not None else 0.0
    if first_data is not None and last > 0:
        # 「数据帧全挤在末尾」的判据：从首数据帧到末帧的跨度远小于整段时长，
        # 且响应头本身也是拖到很晚才到。这正是攒包的指纹。
        span = last - first_data
        if headers > 1.0 and span < max(1.0, 0.05 * last):
            return (
                "❌ 交付层攒包",
                (
                    f"响应头 {headers:.2f}s 才到（≈ 流全长 {last:.2f}s），数据帧"
                    f"全挤在最后 {span:.2f}s 内。keepalive 注释帧存在但没能让"
                    "中间层放行——它按整包处理。"
                ),
            )
    if len(comments) >= 2:
        return (
            "✅ 真流式",
            (
                f"响应头 {headers:.2f}s 到达，{len(comments)} 个 keepalive 注释"
                f"帧周期性出现，数据帧自 {(first_data or 0):.2f}s 起持续到达"
                f"（跨度 {(last - first_data) if first_data is not None else 0:.2f}s）。"
            ),
        )
    return (
        "⚠ 证据不足",
        "注释帧少于 2 个、且未呈现攒包指纹——任务可能太短，请用更长的任务重测。",
    )


def _print_report(timeline: Timeline, headers: object, status: int, limit: int) -> None:
    print("=== 响应头 ===")
    print(f"  status = {status}")
    print(f"  t_headers = {timeline.headers_at:.3f}s" if timeline.headers_at is not None
          else "  t_headers = (未记录)")
    for name in ("content-type", "transfer-encoding", "content-encoding",
                 "x-accel-buffering", "cache-control", "connection", "server"):
        if name in headers:  # type: ignore[operator]
            print(f"  {name} = {headers[name]}")  # type: ignore[index]

    print(f"\n=== 帧到达时间线（前 {limit} 条）===")
    for frame in timeline.frames[:limit]:
        mark = "COMMENT" if frame["kind"] == "comment" else "data   "
        print(f"  t={frame['t']:7.2f}s [{mark}] {frame['line'][:110]!r}")  # 打印截断

    comments, data = timeline.comments, timeline.data_frames
    print("\n=== 汇总 ===")
    print(f"  总帧数        = {len(timeline.frames)}（数据 {len(data)} / 注释 {len(comments)}）")
    print(f"  首数据帧      = {timeline.first_data_at()}s")
    print(f"  末帧          = {timeline.last_at()}s")
    if comments:
        print(f"  keepalive 首帧 = {comments[0]['t']}s，末帧 = {comments[-1]['t']}s")

    mark, why = timeline.verdict()
    print("\n=== 结论 ===")
    print(f"  {mark}")
    print(f"  {why}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8010",
                        help="目标服务地址（本地或交付层域名）")
    parser.add_argument("--task", default=DEFAULT_TASK, help="提交的任务文本")
    parser.add_argument("--mode", choices=("create", "resume"), default="create",
                        help="create = POST /api/sessions；resume = GET /stream")
    parser.add_argument("--session-id", help="--mode resume 必填")
    parser.add_argument("--timeout", type=float, default=180.0, help="整段读流超时（秒）")
    parser.add_argument("--limit", type=int, default=14, help="打印前多少条帧")
    parser.add_argument("--json", metavar="PATH", help="把完整时间线另存为 JSON")
    args = parser.parse_args()

    if args.mode == "resume" and not args.session_id:
        parser.error("--mode resume 需要 --session-id")

    import httpx

    timeline = Timeline()
    base = args.base_url.rstrip("/")
    try:
        # trust_env=False：**无视本机 HTTP_PROXY** 裸连。交付层量测必须直连——
        # 走本地代理时拿到的是代理自己的行为（它可能自己攒包/改响应头），
        # 那就把"谁的锅"量错了。项目既有探针（.workbuddy/public_sse_probe.py）
        # 也是这个口径（http.client 裸连）。
        with httpx.Client(
            timeout=httpx.Timeout(args.timeout, connect=15.0), trust_env=False,
        ) as client:
            if args.mode == "create":
                context = client.stream("POST", f"{base}/api/sessions",
                                        json={"task": args.task, "max_steps": 1})
            else:
                context = client.stream(
                    "GET", f"{base}/api/sessions/{args.session_id}/stream",
                    params={"after_seq": -1},
                )
            with context as response:
                timeline.headers_at = timeline.elapsed()
                status = response.status_code
                headers = response.headers
                if status != 200:
                    print(f"HTTP {status}: {response.read()[:400]!r}")
                    return 2
                for chunk in response.iter_raw():
                    timeline.feed(chunk)
    except httpx.HTTPError as error:
        print(f"请求失败：{type(error).__name__}: {error}")
        print("（读流超时通常意味着中间层攒住了整个响应——这本身就是结论）")
        if not timeline.frames:
            print("=== 结论 ===\n  ❌ 无字节：整个超时窗口内一个字节都没收到。")
            return 3

    _print_report(timeline, headers, status, args.limit)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({
                "base_url": base, "mode": args.mode, "status": status,
                "t_headers": timeline.headers_at,
                "headers": {k.lower(): v for k, v in headers.items()},
                "frames": timeline.frames,
                "verdict": timeline.verdict()[0],
            }, handle, ensure_ascii=False, indent=2)
        print(f"\n完整时间线已写入 {args.json}")

    return 0 if timeline.verdict()[0].startswith("✅") else 1


if __name__ == "__main__":
    sys.exit(main())
