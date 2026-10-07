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


def test_review_p3_leftover_particle_attributive_not_governing():
    """P3：keep 后隔助词（来/在）再接「的」仍是定语，不管辖 target。

    「被留下来的旧档案」——「留下」后隔「来」再接「的」，与「留着的东西」
    同为定语形态（keep 修饰其后的名词），此前落入裸后置被判管辖误拦
    （over-block 本体）。修复后放行；裸后置原 pin 不回归。
    """
    assert f("忘记被留下来的旧档案", "旧档案")
    assert q("忘记被留下来的旧档案", "旧档案")
    assert f("忘记新密码留下来的东西", "新密码")  # 紧邻后置隔「来」再「的」同豁免
    assert not f("忘记旧密码，新密码留着", "新密码")  # 裸后置仍管辖（P3-1 pin）


def test_review_p4_word_interior_substring_not_keep():
    """P4-1：词内子串不得充当 keep 意图——「遗留」中的「留」。

    「遗留在」=「遗留」+「在」，「留在」是词内子串误命中，此前被判 keep
    管辖误拦 target（over-block 本体）。修复后放行；真 keep 词「保留在」
    仍管辖（边界对照，防 prev-char 守卫误伤）。前片对照：真 keep「保留」
    紧邻的「保留在」不被豁免；「遗留下来的」同时踩 P3（隔「来」接「的」
    定语）与 P4-1（「遗留」词内子串）两条修复路径。
    """
    assert f("忘记遗留在备份里的旧档案", "旧档案")
    assert f("忘记遗留下来的旧档案", "旧档案")
    assert not f("忘记旧密码，保留在备份里的新密码", "新密码")  # 真 keep 对照


def test_review_p4_last_occurrence_wins():
    """P4-2：target 多次出现时由**最后一次出现**的管辖意图判定（最新表态优先）。

    - 「忘记A，保留B，忘记B」：末次出现被「忘记」管辖 → 放行（over-block
      本体；此前「任一处被保留管辖即拒绝」让先前的保留压过最后的忘记）；
    - P2-1 旧案不回归：「忘记新邮箱旧档，保留新邮箱」末次出现被「保留」
      管辖 → 仍拒绝；
    - 「忘记A，保留B，忘记C」删 B 仍拒绝（B 唯一出现被保留管辖，pin）。
    """
    assert f("忘记A，保留B，忘记B", "B")
    assert q("忘记A，保留B，忘记B", "B")
    assert not f("忘记新邮箱旧档，保留新邮箱", "新邮箱")
    assert not f("忘记A，保留B，忘记C", "B")


def test_review_p4_boundary_pins():
    """P4-3：已披露取舍的稳定性 pin（防回归，非本次红）。

    - 取舍二：「不保留」先命中「保留」→ over-block 拒删（fail-closed 稳定）；
    - KEEP/Keep 大小写：输入 casefold 后命中，keep 豁免/管辖与大小写无关；
    - 取舍四：英文后置 under-block（"forget the old key, keep it" 删 old key
      放行）——测其稳定，不修复；
    - 「保留B忘记B」：forget 意图前缀「保留B」非合法指令前缀 → 无子句 →
      fail-closed 拒删（宁拦勿删，与取舍一同向）。
    """
    assert not f("忘记A，不保留B", "B")
    assert not f("forget the old key, KEEP the new key", "new key")
    assert not f("forget the old key, Keep the new key", "new key")
    assert f("forget the old key, keep it", "old key")
    assert not f("保留B忘记B", "B")
