"""/metrics 进程健康观测面（#612）：采集与 Prometheus 文本渲染。

Diagnostic 面（spec 12 三层观测）：指标是诊断数据，不进 SessionEvent（Event≠Log）。
读取零副作用；单项采集失败 ⇒ 该指标整行缺席（缺席≠占位值），不影响任何业务路径
（不变量 #21 的降级语义）。默认无鉴权，与 /api/health 同一口径（AC5，不引入开关）。

方案依据（#612 票 comment issuecomment-5973786816 + 修订 5974544470）：
BUILD 手写文本端点，不引 prometheus_client——其 multiprocess 模式明确禁用
自定义 collector 与 set_function，恰是本面需要的两个机制；堆口径 = gc 跟踪
对象浅尺寸和（tracemalloc 需启动期开启，默认缺席会让 AC1 的堆曲线空悬），
O(n) 扫描走 worker 线程（AC3「重采样在 worker 线程或 O(1) 读数」）。
"""

from __future__ import annotations

import asyncio
import gc
import sys
import time

# 进程基准：模块导入时点 ≈ 应用启动时点（web.app 在启动早期 import 本模块）。
_START_EPOCH = time.time()
_START_MONOTONIC = time.monotonic()

METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes as wt

    # #563 取证形状移植（D:\i563_diag\server_launcher.py）：argtypes/restype
    # 不设全的话 64 位指针参数会被截断，GetProcessMemoryInfo 静默失败。
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD),
            ("PageFaultCount", wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _PSAPI = ctypes.WinDLL("psapi")
    _KERNEL32 = ctypes.WinDLL("kernel32")
    _KERNEL32.GetCurrentProcess.restype = wt.HANDLE
    _PSAPI.GetProcessMemoryInfo.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
        wt.DWORD,
    ]
    _PSAPI.GetProcessMemoryInfo.restype = wt.BOOL


def _rss_bytes() -> int:
    """进程 RSS 字节：Windows PSAPI WorkingSet / Linux /proc/self/status VmRSS。

    拿不到（系统调用失败或非两平台）抛 OSError ⇒ 调用方按整行缺席降级。
    """
    if sys.platform == "win32":
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        handle = _KERNEL32.GetCurrentProcess()
        if not _PSAPI.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            raise OSError(f"GetProcessMemoryInfo failed: {_KERNEL32.GetLastError()}")
        return int(pmc.WorkingSetSize)

    with open("/proc/self/status", encoding="ascii") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    raise OSError("VmRSS not found in /proc/self/status")


def _heap_gc_bytes() -> int:
    """gc 口径堆字节：GC 跟踪对象浅尺寸之和（worker 线程执行，O(n) 扫描）。

    只计 GC 跟踪容器对象的浅尺寸，**低估**原生/未跟踪内存——它是「堆堆积
    趋势」信号，不是精确堆大小；进程内存的权威曲线仍是 RSS。
    """
    getsizeof = sys.getsizeof
    return sum(getsizeof(obj) for obj in gc.get_objects())


def _gauge(name: str, help_text: str, value: float) -> str:
    return f"# HELP {name} {help_text}\n# TYPE {name} gauge\n{name} {value}\n"


async def collect_process_metrics() -> str:
    """采集全部指标并渲染 Prometheus 文本暴露格式（v0.0.4）。

    逐项 try/except：一项炸 ⇒ 只少那一行，其余照常——端点永远 200，
    绝不返回占位值（AC2/AC4）。
    """
    chunks: list[str] = []

    try:
        chunks.append(_gauge(
            "agent_process_rss_bytes",
            "Resident set size of the agent process "
            "(Windows PSAPI WorkingSet / Linux VmRSS).",
            _rss_bytes(),
        ))
    except OSError:
        pass

    try:
        heap = await asyncio.to_thread(_heap_gc_bytes)
    except Exception:  # noqa: BLE001, S110 - 降级语义：堆采集失败只体现为该指标缺席（AC2 缺席≠占位值）
        pass
    else:
        chunks.append(_gauge(
            "agent_process_heap_gc_bytes",
            "Shallow size sum of GC-tracked objects (undercounts native "
            "memory; heap-growth trend signal, RSS is the authoritative curve).",
            heap,
        ))

    try:
        chunks.append(_gauge(
            "agent_asyncio_tasks_active",
            "Number of live asyncio tasks in the serving event loop.",
            len(asyncio.all_tasks()),
        ))
    except RuntimeError:
        pass

    chunks.append(_gauge(
        "agent_process_start_time_seconds",
        "UNIX epoch seconds when the agent process started.",
        _START_EPOCH,
    ))
    chunks.append(_gauge(
        "agent_process_uptime_seconds",
        "Seconds since the agent process started (monotonic clock).",
        time.monotonic() - _START_MONOTONIC,
    ))

    return "".join(chunks)
