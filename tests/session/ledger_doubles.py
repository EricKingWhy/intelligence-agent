"""Operation Ledger 的测试替身（`#315` 起 `resume_and_launch` 会读它）。

`SessionService._unreconciled_tool_calls` 在每次 resume / create 之前问一句
"这本账上还有欠对账的调用吗"，读的就是 `operation_ledger.list_for_session`。拿
裸 `MagicMock()` 当这个 collaborator 会踩两个坑，而且**两个坑都不会以
"断言失败"出现**：

1. 属性调用返回的是 MagicMock（**不可 await**）⇒ `TypeError: object MagicMock
   can't be used in 'await' expression`，用例在服务里炸掉；
2. 换成 `AsyncMock()` 也没解决：它的返回值是 truthy 的 MagicMock ⇒ 恢复会被这道
   闸门读成"有欠账"而**静默拒绝**（红得看不出原因）。

真实契约是"没有行 ⇒ 空列表"，所以默认替身必须**显式**答 `[]`。这与
`tests/session/conftest.py` 里 `get_wiring`（返回二元组）/ `run_manager.get_active`
（答"没有在途 run"）是同一条纪律：形状按真实契约给，别让替身替被测点说话。
"""

from __future__ import annotations

from unittest.mock import AsyncMock


def idle_operation_ledger() -> AsyncMock:
    """一本"什么都没有"的账：`list_for_session` 答 `[]`（其余方法仍是 AsyncMock）。

    用例要考"suspend 前有欠账 ⇒ 拒绝恢复"时必须**显式覆盖**它
    （`list_for_session=AsyncMock(return_value=[...])`），别指望默认替身。
    """
    ledger = AsyncMock()
    ledger.list_for_session = AsyncMock(return_value=[])
    return ledger
