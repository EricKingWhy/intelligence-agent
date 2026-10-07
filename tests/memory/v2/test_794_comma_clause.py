"""#794：逗号不终止「记住/忘记」显式指令的子句。

中文自然指令「记住X，Y」里逗号之后才是实质内容；旧 `_command_clause` 把 `,`/`，`
当子句终止符，导致 `explicit_remember_matches` 对这类指令返回 False（工具层
`tools.py` 随即 PERMISSION_DENIED）。本文件覆盖 remember / forget 两条共用路径。
"""

from __future__ import annotations

from agent_harness.memory.v2.commands import (
    explicit_forget_matches,
    explicit_remember_matches,
)


def test_comma_does_not_terminate_remember_clause():
    """逗号后的内容应落在指令子句内 → True。"""
    for text, content in (
        ("请记住这条个人信息，我喜欢喝冰美式", "我喜欢喝冰美式"),
        ("记住这件事，我明天要去北京出差", "我明天要去北京出差"),
        ("帮我记一下，我的生日是5月1日", "我的生日是5月1日"),
        ("请记住，我喜欢喝冰美式", "我喜欢喝冰美式"),
        ("please remember this, I like iced americano", "I like iced americano"),
        ("记住：买牛奶，鸡蛋，面包", "鸡蛋"),
    ):
        assert explicit_remember_matches(text, content), text


def test_comma_clause_still_rejects_negation_turn_and_out_of_text():
    """底线：否定、转折、句末截断、内容不在原文，一律仍 False。"""
    # 否定句是全局检查，与子句长度无关。
    assert not explicit_remember_matches("不要记住我的密码", "我的密码")
    # 转折边界（但是）仍然截断子句。
    assert not explicit_remember_matches("记住这件事，但是别记我的密码", "我的密码")
    # 句号仍终止子句，句末内容不得写入。
    assert not explicit_remember_matches(
        "记住我喜欢喝冰美式。我讨厌咖啡", "我讨厌咖啡",
    )
    # content 必须出现在用户原文中。
    assert not explicit_remember_matches("记住我喜欢喝茶", "我喜欢喝咖啡")


def test_comma_does_not_terminate_forget_clause():
    """forget 路径共用同一函数：逗号后是 memory_id 时仍应命中。"""
    assert explicit_forget_matches("请忘记这条记录，我的旧电话是123", "123")
    # 转折边界仍截断，且「别删」是否定。
    assert not explicit_forget_matches("忘记这件事，但是别删我的密码", "我的密码")
