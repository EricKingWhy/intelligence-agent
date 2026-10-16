"""测试用可注入挂钟。

#1220 复审 B6：`ask_user` 超时相关用例共用一个 —— 勿在各测试文件里各写一份同名类
（同名不同形会误导）。`clock()` 返回当前值，`advance(s)` 前进 `s` 秒；也可直接改
`now`（集成用例里显式 tick 的写法）。
"""

from __future__ import annotations


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
