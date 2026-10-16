"""#1222：`ask_user` 归一/渲染——custom-only 作答不得重复渲染。

复现（一手，见 issue #1222）：用户只填自定义行时前端提交 `{custom_text: "<文本>"}`，
归一后 `entry["label"] == entry["custom_text"]`，旧实现无条件追加"（补充：…）"
⇒ 注入消息变成「<文本>（补充：<文本>）」。

本地钉住两件事：
1. custom-only 作答：自定义文本在注入消息里**只出现一次**（无"（补充：…）"）。
2. 选项 + 补充作答：仍保留"（补充：…）"（回归保护，别把正常场景一起改没）。
"""

from __future__ import annotations

from agent_harness.session import USER_INPUT_REQUESTED, SessionEvent
from agent_harness.session.service import _ask_user_input_answer_data

_REQUEST_ID = "req-1222"


def _request(question: str) -> SessionEvent:
    return SessionEvent(
        seq=0,
        type=USER_INPUT_REQUESTED,
        session_id="s1",
        run_id="run-1",
        data={"kind": "ask_user", "questions": [{"question": question}]},
    )


def _normalize(question: str, answer: dict) -> dict:
    data, _replayed = _ask_user_input_answer_data(
        [],
        run_id="run-1",
        request_id=_REQUEST_ID,
        request=_request(question),
        answer={
            "request_id": _REQUEST_ID,
            "questions": [{"question": question, "answer": answer}],
        },
        ask_user_timeout_seconds=300,
    )
    assert data is not None
    return data


def test_custom_only_answer_is_not_rendered_twice() -> None:
    text = "下周一凌晨"
    data = _normalize("什么时候出发？", {"custom_text": text})

    content = data["content"]
    assert content.count(text) == 1
    assert "（补充：" not in content
    assert content == f"用户回答了提问：\n- 什么时候出发？：{text}"
    # 归一后的答案本身不变（custom-only 仍以自定义文本为 label）。
    assert data["input_request_answer"]["questions"] == [
        {"question": "什么时候出发？", "answer": {"label": text, "custom_text": text}}
    ]


def test_option_with_distinct_custom_text_keeps_supplement() -> None:
    data = _normalize("什么时候出发？", {"label": "选项A", "custom_text": "补充说明"})

    content = data["content"]
    assert content == "用户回答了提问：\n- 什么时候出发？：选项A（补充：补充说明）"
