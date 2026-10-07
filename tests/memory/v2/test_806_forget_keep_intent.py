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
    assert q("忘记旧密码", "旧密码")  # P4-4：query 变体正向断言


def test_review_fixes_all_occurrences_word_family_and_english():
    """独立审查修复批（P2-1/P2-2/P3-1/P4-1）：

    - P2-1：target 多次出现时任一处被「保留」管辖即拒绝（fail-closed），
      不只看首次出现；
    - P2-2：「留住/留在」与「留着/留下」同词族；
    - P3-1：英文 \\bpreserve\\b / \\bretain\\b；
    - P4-1：空串/空白 memory_id 不放行（与 query 变体对称守卫）。
    """
    assert not f("忘记新邮箱旧档，保留新邮箱", "新邮箱")
    assert not f("忘记旧密码，留住新密码", "新密码")
    assert not f("忘记旧密码，留在新密码", "新密码")
    assert not f("forget the old key, preserve the new key", "the new key")
    assert not f("forget the old key, retain the new key", "the new key")
    assert not f("忘记旧密码", "  ")
    assert not f("忘记旧密码", "")
    assert not q("忘记旧密码", "   ")


def test_latest_intent_reasserts_forget():
    """最近意图重断言：保留 B 之后又明确忘记 C → 删 C 放行、删 B 仍拒绝。"""
    assert f("忘记A，保留B，忘记C", "C")
    assert not f("忘记A，保留B，忘记C", "B")
    assert f("forget A, keep B, forget C", "C")
    assert not f("forget A, keep B, forget C", "B")


def test_postposition_keep_governs_target():
    """P3-1：keep 意图**紧邻后置**于 target 也构成管辖 → 拒绝删除。

    - 「新密码留着」：keep 紧跟 target 之后，管辖 target（fail-open 修复本体）；
    - 「把新密码留着」：把字结构的 keep 动词同样紧邻 target，一并拒绝；
    - 「把新密码留着，忘记旧密码」：忘记前的逗号重置子句前缀，target 不落在
      子句内，本就 False（回归 pin，不是本次修复的红）；
    - over-block 边界（把字句误管辖也是 bug）：keep 管辖他物时**不得**误拦
      target——「把旧的留着」管辖「旧的」、keep 与 target 之间有间隔（逗号、
      其他名词）一律放行；
    - 正常删除、前置 keep 均不受影响。
    """
    # 红：后置 keep 构成管辖 → 拒绝。
    assert not f("忘记旧密码，新密码留着", "新密码")
    assert not q("忘记旧密码，新密码留着", "新密码")
    assert not f("忘记旧密码，把新密码留着", "新密码")
    assert not f("把新密码留着，忘记旧密码", "新密码")  # 逗号重置子句前缀（pin）
    # 正常删除不受影响。
    assert f("忘记旧密码", "旧密码")
    assert f("忘记旧邮箱，保留新邮箱", "旧邮箱")
    # over-block 边界：keep 管辖他物或与 target 有间隔 → 不误拦。
    assert f("忘记新密码，把旧的留着", "新密码")
    assert f("忘记新密码，新钥匙留着", "新密码")
    assert f("忘记新密码，留着新钥匙", "新密码")


def test_postposition_keep_attributive_form_does_not_govern():
    """审查清零 P3：定语形态的后置 keep 不管辖 target → 放行删除。

    「留着的东西」「留下的内容」里 keep 词后紧跟「的」是定语标志（keep 修饰
    其后的名词，不作用于 target「新密码」），与已披露的「把旧的留着」同类
    over-block；裸后置（后随字符非「的」）仍构成管辖，P3-1 原 pin 不回归。
    """
    assert f("忘记新密码留着的东西", "新密码")
    assert q("忘记新密码留着的东西", "新密码")
    assert f("忘记新密码留下的内容", "新密码")
    assert not f("忘记旧密码，新密码留着", "新密码")
    assert not q("忘记旧密码，新密码留着", "新密码")
