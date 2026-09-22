#!/usr/bin/env python
"""B8 (#281) 量测：流式 chunk 聚合「抄了多少字符」与耗时（改造前 / 改造后同一口径）。

## 它回答的问题

`_drive` 把流式 chunk 聚合成一条完整 `AIMessage`。改造前是逐 chunk `+`（`ai = ai + c`）：
LangChain 的 `AIMessageChunk.__add__` 每步都**新建**一条消息、把 content 合并一次，
第 i 步要重抄一遍已累计的 i 个 chunk ⇒ 总量 ∝ N·L（二次）。改造后把其余 chunk 交给
**一次**官方归并（`AIMessageChunk.__add__` 的 list 形态 → `add_ai_message_chunks`）
⇒ 累计内容只抄一次 ⇒ 总量 ∝ L（线性）。

## 口径（PERF_BASELINE §2.3）

1. **确定性指标（不含计时）**：聚合全程交给 `merge_content` 的**复制量**——str content 计
   **字符数**（§1/§2 用它）、块列表 content 计**元素数**（§1b 用它）。这是 O(N·L) vs O(L) 的
   **直接**度量，与机器无关 ⇒ 它是主判据。
2. **耗时**：同一批 N 的墙钟（best-of-5，取最小值抗抖动）。只作上下文：它由 memcpy 带宽、
   GC 与缓存共同决定，跨机器漂移可达数十个百分点。
3. **端到端**：真跑 `AgentRuntime.run_stream`（剧本 = N 个文本 chunk），中位数 3 次。
   ⚠ 这一段的读数取决于**当前树里装的是哪版 `runtime.py`**——它是唯一与树相关的量。

## 判定规则

N ×4 时：**复制量比 ≈4 ⇒ 线性；≈16 ⇒ 二次**。耗时同向（但被常数项掩盖，见 §4）。

## 复现

改造后（本树）：

    PYTHONUTF8=1 .venv/Scripts/python.exe scripts/measure_chunk_aggregation.py after

改造前：**同一条命令**，但在独立副本里把 `runtime.py` 换回改造前版本（§3 才走真实代码路径）：

    git show <改造前 commit>:src/agent_harness/agent/runtime.py > <副本>/src/agent_harness/agent/runtime.py

§1 / §2 的两列**就是** A/B（同一台机器、同一次运行；两条表达式内联在本文件里，与树无关），
§3 只有一列。
"""

from __future__ import annotations

import asyncio
import pathlib
import statistics
import sys
import tempfile
import time
from typing import Any, Self

import langchain_core.messages.ai as lc_ai
from langchain_core.messages import AIMessage, AIMessageChunk

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from agent_harness.agent import AgentRuntime
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session

#: 票面场景：30k 字符被切成数千 chunk ⇒ 每 chunk ~10 字符。
WIDTH = 10
SIZES = (1000, 4000, 16000)


def naive_fold(chunks: list[AIMessageChunk]) -> AIMessage:
    """改造前 `_drive` 的聚合形态：逐 chunk `+`（逐字复刻，含类型对齐那一步）。"""
    ai: AIMessage = chunks[0]
    for c in chunks[1:]:
        ai = ai + c  # type: ignore[assignment]
    if not isinstance(ai, AIMessage):
        ai = AIMessage(content=ai.content, tool_calls=ai.tool_calls)  # type: ignore[arg-type]
    return ai


def oneshot_fold(chunks: list[AIMessageChunk]) -> AIMessage:
    """改造后 `_drive` 的聚合形态：其余 chunk 交给一次官方归并。"""
    ai: AIMessage = chunks[0]
    if chunks[1:]:
        ai = ai + chunks[1:]  # type: ignore[assignment]
    if not isinstance(ai, AIMessage):
        ai = AIMessage(content=ai.content, tool_calls=ai.tool_calls)  # type: ignore[arg-type]
    return ai


class _CopyVolume:
    """数聚合期间交给 `merge_content` 的字符总数（= 聚合一共抄了多少字符）。

    `add_ai_message_chunks` 调用的是模块全局名 `merge_content`，因此在模块上替换即可
    同时覆盖两条臂（逐项 `+` 也走同一个入口）。

    **两种 content 形态分列计**（混成一个总数没有意义）：
    - `chars`：str content 的字符数——**§1/§2 用的就是它**（票面场景 = 「30k 字符切成数千
      chunk」= 纯文本）；
    - `blocks`：块列表 content（`[{"type": "text", ...}]`）的**元素个数**——LangChain 对这种
      content 走 `merge_lists`，复制成本按元素计，此时 `chars` 恒为 0（不是"没抄东西"）。
    只计 str 或只计元素都不会给出错误结论，但**哪一列有含义取决于语料**，故两列并列。
    """

    def __init__(self) -> None:
        self.chars = 0
        self.blocks = 0
        self._real: Any = None

    def __enter__(self) -> Self:
        self._real = lc_ai.merge_content

        def counting(first: Any, *others: Any) -> Any:
            for part in (first, *others):
                if isinstance(part, str):
                    self.chars += len(part)
                elif isinstance(part, (list, tuple)):
                    self.blocks += len(part)
            return self._real(first, *others)

        lc_ai.merge_content = counting
        return self

    def __exit__(self, *exc: object) -> None:
        lc_ai.merge_content = self._real


def shape(msg: AIMessage) -> tuple[str, Any, Any, Any, Any]:
    """两条臂必须聚出同一条消息——量测前先自证 A/B 是同一件事。"""
    return (type(msg).__name__, msg.content, msg.tool_calls,
            getattr(msg, "usage_metadata", None), msg.id)


def text_chunks(n: int, width: int) -> list[AIMessageChunk]:
    return [AIMessageChunk(content="x" * width) for _ in range(n)]


def block_chunks(n: int) -> list[AIMessageChunk]:
    """块列表 content——LangChain 对它的合并走 `merge_lists`，复制成本按**元素**计。"""
    return [AIMessageChunk(content=[{"type": "text", "text": "x"}]) for _ in range(n)]


def volume(fn: Any, chunks: list[AIMessageChunk]) -> tuple[int, int]:
    """返回 `(chars, blocks)`——哪一列有含义取决于语料（见 `_CopyVolume`）。"""
    with _CopyVolume() as vol:
        fn(list(chunks))
    return vol.chars, vol.blocks


def best_ms(fn: Any, chunks: list[AIMessageChunk], repeats: int = 5) -> float:
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn(list(chunks))
        best = min(best, time.perf_counter() - t0)
    return best * 1000.0


class _Scripted:
    """按剧本吐 chunk 列表的模型替身（与仓库测试里的替身同形）。"""

    def __init__(self, chunks: list[AIMessageChunk]) -> None:
        self._chunks = chunks

    async def astream(self, messages: Any) -> Any:
        for c in self._chunks:
            yield c

    async def ainvoke(self, messages: Any) -> AIMessage:
        raise AssertionError("本脚本只走流式")


async def end_to_end(n: int, width: int, repeats: int = 3) -> float:
    times: list[float] = []
    for i in range(repeats):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"iab-b281-e2e-{i}-"))
        registry = ToolRegistry()
        runtime = AgentRuntime(model=_Scripted(text_chunks(n, width)), registry=registry,
                               executor=ToolExecutor(registry), max_steps=5)
        session = make_session(tmp)
        t0 = time.perf_counter()
        async for _ in runtime.run_stream(session, "hi"):
            pass
        times.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(times)


def ratio(now: float, prev: float | None) -> str:
    return f"{now / prev:.2f}x" if prev else "-"


async def main() -> None:
    import agent_harness

    print(f"src 落点: {agent_harness.__file__}")
    label = sys.argv[1] if len(sys.argv) > 1 else "?"
    print(f"=== B8 #281 基准（{label}）：chunk 聚合 复制量 / 耗时 / 端到端 ===")

    print(f"\n【1】确定性指标：聚合全程交给 merge_content 的字符总数（width={WIDTH}，纯文本语料）")
    print(f"{'N':>7}{'流总字符 L':>12}{'naive 复制量':>15}{'oneshot 复制量':>16}"
          f"{'naive/L':>10}{'naive 4×比':>12}{'oneshot 4×比':>14}")
    prev_n: float | None = None
    prev_o: float | None = None
    for n in SIZES:
        chunks = text_chunks(n, WIDTH)
        assert shape(naive_fold(chunks)) == shape(oneshot_fold(chunks)), f"N={n}: 两臂结果不同"
        v_n, _ = volume(naive_fold, chunks)
        v_o, _ = volume(oneshot_fold, chunks)
        print(f"{n:>7}{n * WIDTH:>12}{v_n:>15,}{v_o:>16,}{v_n / (n * WIDTH):>9.2f}x"
              f"{ratio(v_n, prev_n):>12}{ratio(v_o, prev_o):>14}")
        prev_n, prev_o = v_n, v_o
    print("  判据：4× N 时 ≈4 ⇒ 线性；≈16 ⇒ 二次。naive/L 随 N 线性增长即二次的直接证据。")
    print("  L = 流的总字符数（= 改造后应当抄的量）；改造后列恒等于 L 即 O(L)。")

    print("\n【1b】同一口径的块列表语料（`content=[{type:text,...}]`）：此语料下 chars 恒为 0")
    print(f"{'N':>7}{'流总元素':>10}{'naive 元素数':>14}{'oneshot 元素数':>16}"
          f"{'naive 4×比':>12}{'oneshot 4×比':>14}")
    prev_n = prev_o = None
    for n in SIZES:
        chunks = block_chunks(n)
        assert shape(naive_fold(chunks)) == shape(oneshot_fold(chunks)), f"N={n}: 两臂结果不同"
        _, b_n = volume(naive_fold, chunks)
        _, b_o = volume(oneshot_fold, chunks)
        print(f"{n:>7}{n:>10}{b_n:>14,}{b_o:>16,}{ratio(b_n, prev_n):>12}{ratio(b_o, prev_o):>14}")
        prev_n, prev_o = b_n, b_o
    print("  形态判据看本表的**元素**列：这一列与 §1 同形（改造前二次、改造后线性）。")

    print(f"\n【2】耗时（同一批 N，best-of-5；width={WIDTH}）")
    print(f"{'N':>7}{'naive(ms)':>12}{'oneshot(ms)':>14}{'倍数':>10}"
          f"{'naive 4×比':>12}{'oneshot 4×比':>14}")
    prev_n = prev_o = None
    for n in SIZES:
        chunks = text_chunks(n, WIDTH)
        t_n, t_o = best_ms(naive_fold, chunks), best_ms(oneshot_fold, chunks)
        print(f"{n:>7}{t_n:>12.1f}{t_o:>14.2f}{t_n / t_o:>9.1f}x"
              f"{ratio(t_n, prev_n):>12}{ratio(t_o, prev_o):>14}")
        prev_n, prev_o = t_n, t_o

    print(f"\n【3】端到端 run_stream（当前树的 runtime.py；width={WIDTH}，中位数 3 次）")
    print(f"{'N':>7}{'中位数(ms)':>14}")
    for n in SIZES:
        print(f"{n:>7}{await end_to_end(n, WIDTH):>14.1f}")

    print("\n【4】宽 chunk（width=200）：把「重抄已累计内容」的代价放大 ⇒ 二次项主导墙钟")
    print(f"{'N':>7}{'流总字符':>10}{'naive(ms)':>12}{'oneshot(ms)':>14}{'naive 翻倍比':>14}")
    prev_n = None
    for n in (1000, 2000, 4000, 8000):
        chunks = text_chunks(n, 200)
        t_n, t_o = best_ms(naive_fold, chunks, 3), best_ms(oneshot_fold, chunks, 3)
        print(f"{n:>7}{n * 200:>10}{t_n:>12.1f}{t_o:>14.1f}"
              f"{(f'{t_n / prev_n:.2f}x' if prev_n else '-'):>14}")
        prev_n = t_n
    print("  （N 翻倍：naive 趋近 4× ⇒ 二次项主导；oneshot 趋近 2× ⇒ 线性）")
    print("  ⚠ 8000 那档**两臂都跳**（工作集 1.6 MB 的分配/GC 悬崖）——该点只作上下文；")
    print("     形态判据看 §1（确定性）与 §2（1k/4k/16k）。")


if __name__ == "__main__":
    asyncio.run(main())
