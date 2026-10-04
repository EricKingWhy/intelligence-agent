"""W-22（#366）：产品客户端在场闸门——`client_absent` 暂停的最小 Runtime 接缝。

判据来源：`02 §5.2.1`（只对已纳入产品客户端在场协议的 Run 生效；最后客户端明确
退出或断线宽限到期后阻止新的 Model/Tool/Child 接纳）、`11 §6.2`（协议只接管明确
登记的 Task；宽限与注册属 W-12）、ADR-0046。

**职责边界（W-22 vs W-12）**：本模块只承载"这一个 run 的客户端还在不在"这一个
布尔事实与它的置位口。谁算客户端、登记 / 心跳 / 明确退出与意外断线的区分、30 秒
宽限配置、多客户端同 Task——全部属 W-12 的注册协议（`docs/tickets/.../W-12`）。
生产装配里今天没有人 `enroll()`（W-12 落地前所有 run 都是未登记 run，行为与
W-22 之前逐字相同）；RunManager 的在场管理接缝（`presence_managed`）是唯一的
登记入口，孤儿回收计时器到期时置缺席。

单事件循环内使用（与 Runtime / RunManager 同一 loop），不做跨线程同步。
"""

from __future__ import annotations


class ClientPresenceGate:
    """一个 run 的客户端在场闸门：惰性直到被登记，登记后可被置为缺席。

    三态语义刻意压成两个布尔：`managed`（是否纳入在场协议）与 `absent`
    （协议内且已判定缺席）。**未登记 ⇒ `absent` 恒 False**——即使被误调用
    `mark_absent()`（防御性 no-op）：旧 Web / CLI 的 run 绝不能因为一条为
    登记 run 准备的信号而暂停，那是 `11 §6.2`「协议只接管明确登记的 Task」
    在闸门这一层的形状。
    """

    __slots__ = ("_absent", "_managed")

    def __init__(self) -> None:
        self._managed = False
        self._absent = False

    def enroll(self) -> None:
        """把本 run 纳入产品客户端在场协议（幂等）。

        调用方是 RunManager 的在场管理接缝（`launch(presence_managed=True)`）；
        W-12 的注册协议落地后仍走同一入口，不另开第二条登记路径。"""
        self._managed = True

    def mark_absent(self) -> None:
        """判定"最后一个产品客户端已离开"（明确退出或断线宽限到期）。

        单向：在场协议没有"自动回来"——客户端回归走显式 resume
        （`resume_basis=client_return`，`11 §6.2`：重连本身不自动恢复），
        恢复执行是**新的执行段**（新 Runtime / 新闸门），不是把这里翻回去。
        未登记的 run 上是 no-op（见类 docstring）。"""
        if self._managed:
            self._absent = True

    @property
    def managed(self) -> bool:
        """本 run 是否纳入在场协议（未纳入 = W-22 之前的行为逐字不变）。"""
        return self._managed

    @property
    def absent(self) -> bool:
        """循环顶准入点读的唯一判据：已登记且已判定缺席。"""
        return self._managed and self._absent
