"""#650 回归：孤立 Unicode surrogate 在 Context 预算边界的受控拒绝。

票面 T6f（issue #650）：``estimate_message_tokens`` 对含孤立代理项的消息
裸抛 ``PydanticSerializationError``（``model_dump_json`` 无法把 U+D800–U+DFFF
编码为 JSON）。修复契约：预算边界映射为 ``ContextWindowExceededError``
（spec 06 §8 hard guard 语义：停止或交用户处理，不放行、不静默降级），
非 surrogate 原因的序列化失败原样上抛、不混类。

正控（AC3）：Python ``str`` 里两段 surrogate（``chr(0xD800) + chr(0xDC00)``）
**不构成**合法标量对——Python 字符串把 astral 字符存为单个码点，任何落在
代理区间的码点都是孤立的，仍必须拒绝。

AC4：错误消息不回显用户内容；合法标量（emoji / 中文 / 换行控制转义）
照常准确计数，不丢字符。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from pydantic_core import PydanticSerializationError

from agent_harness.context.compactor import ContextWindowExceededError
from agent_harness.context.tokens import estimate_message_tokens


def _message_tokens(text: str) -> int:
    return estimate_message_tokens([HumanMessage(content=text)])


class TestLoneSurrogateControlledRejection:
    @pytest.mark.parametrize(
        ("label", "text"),
        [
            ("high_surrogate", chr(0xD800)),
            ("low_surrogate", chr(0xDC00)),
            # AC3 正控：两段 surrogate 拼在一个 Python 字符串里仍不是合法标量
            ("two_halves_one_str", chr(0xD800) + chr(0xDC00)),
        ],
    )
    def test_lone_surrogate_maps_to_context_window_exceeded(self, label: str, text: str):
        with pytest.raises(ContextWindowExceededError):
            _message_tokens(text)

    def test_error_message_does_not_echo_user_content(self):
        marker = "SECRET-" + chr(0xD800) + "-PAYLOAD"
        with pytest.raises(ContextWindowExceededError) as excinfo:
            _message_tokens(marker)
        assert "SECRET-" not in str(excinfo.value)


class TestNonSurrogateFailureNotConflated:
    """审查 P3（双轴独立指出）：tokens.py「原样上抛」分支无回归钉。

    只有「含孤立代理项」的序列化失败才映射成 ``ContextWindowExceededError``；
    非 surrogate 原因（``additional_kwargs`` 携带 pydantic 无法 JSON 序列化的
    对象）必须保持 ``PydanticSerializationError`` 原样上抛。若未来把所有序列
    化失败一律映射成 CWE，现有 surrogate 用例仍全绿——本钉使混类回归变红
    （CWE 是 RuntimeError，``pytest.raises(PydanticSerializationError)``
    不会误捕）。
    """

    def test_unknown_type_failure_reraised_not_mapped(self):
        message = HumanMessage(
            content="clean text", additional_kwargs={"blob": object()}
        )
        with pytest.raises(PydanticSerializationError) as excinfo:
            estimate_message_tokens([message])
        assert not isinstance(excinfo.value, ContextWindowExceededError)


class TestLegalScalarsStillCounted:
    """AC3：真实 Unicode 标量不受影响，准确计数（读数与修前探针一致）。"""

    def test_emoji_scalar_counted(self):
        assert _message_tokens(chr(0x1F600)) == 28

    def test_chinese_and_escape_sequences_counted(self):
        assert _message_tokens("你好\n世界") == 32

    def test_ascii_message_counted(self):
        assert _message_tokens("hello world") == 28
