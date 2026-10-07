"""#806：forget guard 必须尊重「保留」反向意图。

#794 让 `_command_clause` 的子句跨过逗号后，「忘记旧邮箱，保留新邮箱」里的
「新邮箱」也落进子句，forget guard 会放行删除——尽管用户亲口说了「保留」。
本文件约束反向意图检查：target 之前最近的意图是 keep（而非 forget）→ 拒绝，
且不做成全局否定（正常删除不受影响）。
"""

from __future__ import annotations

from agent_harness.memory.v2.commands import (
    explicit_forget_matches as f,
)
from agent_harness.memory.v2.commands import (
    explicit_forget_query_matches as q,
)


def test_keep_intent_blocks_forget_of_kept_target():
    """#806 本体：用户说了「保留新邮箱」，guard 不得放行删「新邮箱」。"""
    assert not f("忘记旧邮箱，保留新邮箱", "新邮箱")
    assert not f("忘记旧邮箱，但是保留新邮箱", "新邮箱")  # 转折已保护，双保险
    assert not q("忘记旧邮箱，保留新邮箱", "新邮箱")
    assert not f("forget the old email, keep the new one", "new one")


def test_normal_forget_unaffected():
    """不做全局否定：正常删除仍放行。"""
    assert f("忘记旧邮箱，保留新邮箱", "旧邮箱")
    assert f("请忘记这条记录，我的旧电话是123", "123")
    assert f("忘记旧密码", "旧密码")


def test_latest_intent_reasserts_forget():
    """最近意图重断言：保留 B 之后又明确忘记 C → 删 C 放行、删 B 仍拒绝。"""
    assert f("忘记A，保留B，忘记C", "C")
    assert not f("忘记A，保留B，忘记C", "B")
    assert f("forget A, keep B, forget C", "C")
    assert not f("forget A, keep B, forget C", "B")
