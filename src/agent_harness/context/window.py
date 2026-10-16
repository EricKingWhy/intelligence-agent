"""会话级上下文窗口投影（#1197 项 1）。

窗口是**会话级**配置：同一进程里两个会话可以各选各的挡位（票面「per-session 独立」）。
存储形态照抄 :mod:`agent_harness.session.workflow` 的三件套（用户裁决 A 明令
「复用仓库统一 per-session 形状……照着抄，不要发明新约定」）：

- ``effective_context_window`` —— 倒序扫 ``context/window-changed``，最后一次胜；
  坏值 warning 后继续往下找，无事件回落 :data:`DEFAULT_CONTEXT_WINDOW`。
- ``append_context_window_change`` —— ``CONTEXT_WINDOW_CHANGED`` 的**唯一**写入口。

为什么不是 ``session/started`` 的一个键：窗口要能在会话跑到一半时改（票面
「会话内下一轮上下文构建即生效」），而 ``session/started`` 是创建期一次性事实。
为什么不是 ``SessionMetaStore`` 加一列：那是**元数据索引**（列表/归档/checkpoint），
把运行期配置塞进去会让「改配置」变成一次索引写，且 derive 语义（last-wins）还得
在别处重写一遍。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent_harness.config import CONTEXT_WINDOW_TIERS
from agent_harness.session.event import CONTEXT_WINDOW_CHANGED, SessionEvent

if TYPE_CHECKING:
    from agent_harness.session.session import Session

logger = logging.getLogger("agent_harness.context.window")

#: 无 ``context/window-changed`` 事件时的回落值。字面量与
#: ``Settings.max_context_tokens`` 的默认**分列两处**（config 是依赖图底层，
#: context 反向依赖它没问题，但窗口的**默认**语义属于会话投影这一层）——两者相等由
#: ``tests/context/test_context_window.py::test_default_is_the_settings_default`` 钉住。
DEFAULT_CONTEXT_WINDOW: int = 200_000


def effective_context_window(
    events: list[SessionEvent], *, default: int = DEFAULT_CONTEXT_WINDOW,
) -> int:
    """派生会话**当下生效**的上下文窗口（token）。

    投影范式与 :func:`agent_harness.session.workflow.effective_workflow_mode` 一致：
    倒序扫、last-wins。命中 ``context/window-changed`` 取 ``data["context_window"]``；
    **坏值**（非 int / bool / 不在挡位表内）记 warning 并继续往下找——不猜一个
    「差不多」的挡位，猜错会让 70%/85% 线落在用户没选过的位置上。
    无合法事件时回落 ``default``。

    ``default`` 是**部署级**默认（调用方传 ``Settings.max_context_tokens``），不是
    :data:`DEFAULT_CONTEXT_WINDOW` 那个字面量——部署者在 .env 里设了
    ``MAX_CONTEXT_TOKENS=500000`` 时，会话没改过挡位就该是 500K；把回落写死成
    200K 会让部署配置被静默忽略（面板显示 500K、实际按 200K 压缩）。
    """
    for event in reversed(events):
        if event.type != CONTEXT_WINDOW_CHANGED:
            continue
        raw = event.data.get("context_window")
        # bool 是 int 的子类：`True in CONTEXT_WINDOW_TIERS` 为 False，无需单列，
        # 但显式写出来免得后来人以为漏了。
        if isinstance(raw, int) and not isinstance(raw, bool) and raw in CONTEXT_WINDOW_TIERS:
            return raw
        logger.warning(
            "context/window-changed 的 context_window=%r 不是合法挡位，跳过继续查找", raw
        )
    return default


def append_context_window_change(session: Session, context_window: int) -> int:
    """追加 ``context/window-changed``——该事件的**唯一**写入口（#1197 项 1）。

    写入口把守挡位（挡位外的值不许进事件日志，否则 derive 侧只能靠坏值分支兜住，
    而坏值分支的语义是"忽略"、不是"拒绝"——静默忽略用户的选择是最坏的形态）。
    走 ``Session.append``（append-only，不产生 resume 副作用）。
    """
    if context_window not in CONTEXT_WINDOW_TIERS:
        raise ValueError(
            f"context_window 必须是 {list(CONTEXT_WINDOW_TIERS)} 之一，得到 {context_window!r}"
        )
    session.append(CONTEXT_WINDOW_CHANGED, {"context_window": context_window})
    return context_window


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "append_context_window_change",
    "effective_context_window",
]
