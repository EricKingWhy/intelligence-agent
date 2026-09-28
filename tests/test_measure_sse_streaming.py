"""交付层测量脚本的判定逻辑。

`scripts/measure_sse_streaming.py` 是**决策工具**：用户靠它的结论决定"要不要为
流式换掉整条客户端传输"。一个只会说"✅ 正常"的诊断工具比没有更糟——它会把真实
故障粉饰成通过，于是人被引向错误的方向。所以这里对着**合成时间线**把两条分支都
钉死：真流式（阶梯到达 + keepalive 周期出现）与攒包（响应头拖到流末、数据帧挤在
最后）。

判定只看观测事实（帧到达时刻 + 响应头时刻），不看中间层实现——因此合成时间线
就足以覆盖它的全部输入域。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure_sse_streaming.py"


def _load_module():
    """按路径加载脚本（它不在包内，也没有 CLI 副作用——main() 在 __main__ 下才跑）。"""
    spec = importlib.util.spec_from_file_location("measure_sse_streaming", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def measure():
    return _load_module()


def _frame(t: float, kind: str) -> dict:
    return {"t": t, "kind": kind, "line": ": ping" if kind == "comment" else "data: {}"}


def test_streaming_timeline_reads_as_streaming(measure):
    """真流式：响应头立刻到、数据帧阶梯式到达、keepalive 周期夹在中间。"""
    frames = [
        _frame(0.1, "data"), _frame(0.5, "data"),
        _frame(2.0, "comment"), _frame(2.5, "data"),
        _frame(4.0, "comment"), _frame(4.6, "data"),
        _frame(6.0, "comment"), _frame(6.4, "data"),
    ]
    mark, why = measure.compute_verdict(frames, headers_at=0.05)
    assert mark == "✅ 真流式", why


def test_buffered_timeline_reads_as_gateway_buffering(measure):
    """攒包指纹：响应头拖到 ≈ 流全长才到，数据帧全挤在其后的一小段里。

    这正是部署实测到的形状（响应头 44.158s ≈ run 全长，326 帧随后 0.094s 到齐）。
    判定必须把它读成"交付层攒包"，而不是因为"帧都收到了"就报成功。
    """
    frames = [_frame(44.0, "comment")] + [
        _frame(44.0 + i * 0.01, "data") for i in range(20)
    ]
    mark, why = measure.compute_verdict(frames, headers_at=44.1)
    assert mark == "❌ 交付层攒包", why


def test_missing_keepalive_is_flagged(measure):
    """没有任何注释帧 → 明确指出 keepalive 没上线（本仓应恒有）。"""
    frames = [_frame(0.1, "data"), _frame(5.0, "data")]
    mark, why = measure.compute_verdict(frames, headers_at=0.05)
    assert mark == "⚠ 无 keepalive", why


def test_no_bytes_at_all(measure):
    mark, why = measure.compute_verdict([], headers_at=None)
    assert mark == "❌ 无字节", why


def test_short_task_is_reported_as_inconclusive(measure):
    """任务太短（注释帧只出现一次、也没有攒包指纹）→ 不下结论，要求重测。

    宁可说"证据不足"也不要猜：这条判定的输出会被当成"要不要重做传输层"的依据。
    """
    frames = [_frame(0.1, "data"), _frame(0.3, "comment"), _frame(0.6, "data")]
    mark, why = measure.compute_verdict(frames, headers_at=0.05)
    assert mark == "⚠ 证据不足", why
