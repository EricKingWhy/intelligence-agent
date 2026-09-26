"""per-tool 配额的**准入窗口**（`#314` T6）。

它不是第二本账。账永远是 append-only 事件（`tool/result.data.budget_delta`，
由 `agent/run_budget.py` 按**唯一计数点**求和）；本对象只是"这批调用还能不能收"
的**瞬态判据**：AgentRuntime 在派发一批 `tool_call` 之前按 run 账本构造它，
`ToolExecutor` 在接纳点查询并登记，批次结束即弃。

为什么需要它（而不是让 Executor 每次自己去读事件）：
  一次模型决策可以给出**多条**对同一工具的调用，而 run 配额是跨批次的累计值。
  只在"批次之前"判一次会放过同批里的第 N+1 条——并发只读批次里它们同时开跑
  （`execute_batch` 的 parallel 分支），谁也看不到谁的计数。窗口把"批次开始前的
  账"与"本批已接纳的数"合在一起，用**同步**的检查+预留保证并发批次恰好收下
  ceiling 允许的那几条。

三条刻意定下的边界：

- **预留发生在 approval 之前**：一个注定被配额拒的调用不该先弹一次人工审批
  （审批可能要等人一个回合）。被 approval 拒的调用由调用方 `release()` 归还槽位
  ——否则同批后面那条本该被收下的调用会被一个**没被接纳**的兄弟调用挤掉，而
  账本此刻仍是 0 消耗，"被拒"与"配额已尽"就对不上（暂停判定读的是账本）。
- **未执行/被取消的调用不占用**：`ToolExecutor` 只在真正接纳时 `take()`，
  批次熔断而"未执行"的兄弟调用（`_cancel_without_execution`）根本没走到这里。
- **窗口只管 ceiling**：`limits` 里没有的工具名 = 不限（`04 §9.1`：未配置配额
  不限，但仍计入 `tool_calls` / `tool_attempts`），所以空窗口恒不挡。

构造它的是 AgentRuntime，判定/记数的**唯一判定点**是 `ToolExecutor.execute()`
的接纳闸门；本模块不含任何持久化或事件语义（那些在 `agent/run_budget.py`）。
"""

from __future__ import annotations

from collections.abc import Mapping


class ToolQuotaWindow:
    """一批工具调用的准入窗口：run 账本快照 + 本批已接纳数。

    不是线程安全的、也不该被跨批次复用——每个批次一个实例（生命周期由
    AgentRuntime 的 `execute_batch` 调用点拥有）。
    """

    __slots__ = ("_consumed", "_limits")

    def __init__(
        self,
        *,
        limits: Mapping[str, int],
        consumed: Mapping[str, int],
    ) -> None:
        # 拷贝入参：窗口要在批次内**改**这两张表，绝不回写调用方的账（账是事件派生值）。
        self._limits = {name: int(ceiling) for name, ceiling in limits.items()}
        self._consumed = {name: int(used) for name, used in consumed.items()}

    def ceiling(self, tool_name: str) -> int | None:
        """该工具的绝对上限；None = 本 run 没给它配配额（不限）。"""
        return self._limits.get(tool_name)

    def used(self, tool_name: str) -> int:
        """此刻已计入的调用数（批次前的账 + 本批已接纳的数）。"""
        return self._consumed.get(tool_name, 0)

    def take(self, tool_name: str) -> bool:
        """尝试占一个槽位：挡住 ⇒ False（调用方据此在接纳前拒绝这条调用）。

        返回 True 表示槽位**已占用**——调用方随后必须走 `release()` 或真正接纳，
        不能中途无声丢弃（那会让本批后续调用被一个不存在的计数挤掉）。
        """
        ceiling = self._limits.get(tool_name)
        if ceiling is not None and self.used(tool_name) >= ceiling:
            return False
        self._consumed[tool_name] = self.used(tool_name) + 1
        return True

    def release(self, tool_name: str) -> None:
        """归还一个已占槽位（approval 拒绝等"占了位但没被接纳"的路径）。

        只减不该减到负数：release 的次数由调用方的控制流保证（每个 True 的
        `take()` 恰好对应一次 release 或一次接纳），下界兜底只是防御。
        """
        current = self.used(tool_name)
        if current > 0:
            self._consumed[tool_name] = current - 1
