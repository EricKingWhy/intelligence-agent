"""F16 #235：本地沙箱的 `on_output` 必须是**增量**回调，不能攒到 EOF 一次性给。

为什么需要这条锁：`process.stdout` 是 `BufferedReader`，`read(n)` 会阻塞到凑满 n 字节
或 EOF。常量 `_DRAIN_CHUNK_BYTES = 64 KiB` ⇒ 任何小于 64 KiB 的输出，回调在进程结束前
一次都不会发生，工具卡「输出 · 流式」拿到的是终态全量。判别式必须同时看**次数**与
**时刻**：只看次数的话，"结束时一次给三行"仍可能被数成 1 次而漏判。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from agent_harness.sandbox import LocalSubprocessSandbox


def _three_lines_slowly() -> str:
    """跨平台（本仓两种宿主：win32 cmd / POSIX sh）：三行输出、行间隔约 1s。"""
    if os.name == "nt":
        return "for /L %i in (1,1,3) do @(echo line-%i & ping -n 2 127.0.0.1 >nul)"
    return "for i in 1 2 3; do echo line-$i; sleep 1; done"


def test_on_output_is_incremental_not_buffered_until_eof(tmp_path: Path) -> None:
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path)
    marks: list[tuple[float, str]] = []
    t0 = time.perf_counter()

    def on_output(channel: str, text: str) -> None:
        marks.append((time.perf_counter() - t0, text))

    result = sandbox.exec(_three_lines_slowly(), on_output=on_output)

    # 捕获完整性不受影响（流式是附加通道，不是数据面）
    assert result.exit_code == 0
    assert "line-1" in result.stdout and "line-3" in result.stdout

    assert len(marks) >= 2, (
        f"只有 {len(marks)} 次 on_output 回调——输出被攒到 EOF 一次性给出"
    )
    # 首次回调必须**尚未**包含最后一行（增量而非全量）
    assert "line-3" not in marks[0][1], (
        f"首次回调整段就含 line-3（{marks[0][1]!r}）——不是增量"
    )
    # 且明显早于进程结束（命令约 3s），排除"临结束才第一次回调"
    assert marks[0][0] < 0.8 * marks[-1][0], (
        f"首次 t={marks[0][0]:.2f}s 与末次 t={marks[-1][0]:.2f}s 太接近"
    )
