"""记忆形成的三层降级，不依赖具体 Memory SDK。"""

import asyncio

import pytest
from langchain_core.messages import AIMessage

from agent_harness.memory.extractor import MemoryExtractor
from agent_harness.memory.types import MemoryScope
from agent_harness.session import SessionEvent
from tests.scripted_model import ScriptedModel


def user(content):
    return SessionEvent(seq=0, type="user/message", session_id="s", data={"content": content})


@pytest.mark.asyncio
async def test_extractor_llm_success():
    model = ScriptedModel([AIMessage(content='[{"scope":"user","content":"Prefers TypeScript","importance":0.8}]')])
    assert await MemoryExtractor(model).extract([user("I prefer TypeScript")]) == [
        (MemoryScope.USER, "Prefers TypeScript", {"importance": 0.8}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["bad JSON", '[{"scope":"global","content":"bad","importance":2}]'])
async def test_bad_llm_output_falls_back_to_user_preference(response):
    extractor = MemoryExtractor(ScriptedModel([AIMessage(content=response)]))
    result = await extractor.extract([user("我喜欢 TypeScript")])
    assert result[0][0] == MemoryScope.USER
    assert result[0][1] == "我喜欢 TypeScript"


@pytest.mark.asyncio
async def test_timeout_falls_back_and_cancellation_propagates():
    class Hanging:
        async def ainvoke(self, messages):
            await asyncio.Event().wait()

    extractor = MemoryExtractor(Hanging(), timeout_seconds=0.01)
    assert await extractor.extract([user("nothing useful here")]) == []
    class Cancelled:
        async def ainvoke(self, messages):
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await MemoryExtractor(Cancelled()).extract([user("I prefer TypeScript")])


@pytest.mark.asyncio
async def test_heuristic_preserves_failed_attempts_and_final_decisions():
    events = [SessionEvent(seq=0, type="tool/result", session_id="s",
                           data={"content": '{"ok":false,"message":"file not found"}'}),
              SessionEvent(seq=1, type="run/completed", session_id="s",
                           data={"final_text": "Use the relative path"})]
    result = await MemoryExtractor(ScriptedModel([AIMessage(content="bad")])).extract(events)
    assert [row[0] for row in result] == [MemoryScope.SESSION, MemoryScope.SESSION]
    assert "file not found" in result[0][1]
    assert "relative path" in result[1][1]
