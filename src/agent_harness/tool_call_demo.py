"""Day2 Task1+2：Function Calling 两轮协议 demo。

第一轮：bind_tools 告知 schema -> 模型返回结构化 tool_calls（只观察）。
第二轮（Runtime 手工执行）：校验 args -> 执行 add -> 用相同 tool_call_id 的
ToolMessage 回填 -> 再次调用 -> 模型基于结果生成最终回答。

不接日志体系；model 参数可注入测试替身（ScriptedModel），默认走真实 Provider。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field, ValidationError

from agent_harness.config import Settings
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model


class AddArgs(BaseModel):
    """add 工具的参数结构（schema）。

    这份结构会随请求发给模型，告诉它"调用 add 时需要提供哪些参数"。
    """
    first_number:float = Field(..., description="第一个加数")
    second_number:float = Field(..., description="第二个加数")
    # TODO 1（你写）：定义两个数值参数 a 和 b
    # - 类型用 float（模型对整数的解析最稳）
    # - 每个参数用 Field(...) 写一句清晰的中文描述（description 会影响模型
    #   何时选择这个工具，也影响它填参数的准确性）


async def run(message: str, model=None) -> AIMessage:
    """绑定 add 工具，发送消息，返回模型第一轮的原始 AIMessage。

    model=None 时走真实 Provider；测试时注入 ScriptedModel 替身。
    """
    if model is None:
        settings = Settings()
        config = ModelConfig.from_settings(settings)
        model = create_chat_model(config)
    bound_model = model.bind_tools([
        {"name": "add",
         "description": "计算两个数的和",
         # dict 形式必须是 OpenAI 格式："parameters" 键 + JSON Schema。
         # 写 "args_schema" 会被 LangChain 静默丢弃，schema 根本不会发给模型。
         "parameters": AddArgs.model_json_schema(),
         }
    ],
        strict=True,
    )
    # TODO 2（你写）：把工具绑定到 model 上，得到 bound_model
    # 要求：最终暴露给模型的工具名必须是 "add"（不是 "AddArgs"！）
    # 提示：bind_tools() 接受一个列表，列表里的一项可以是这样的 dict：
    #   {"name": ..., "description": ..., "args_schema": AddArgs}
    # 其中 name 决定 Runtime 以后按什么标识找工具，description 决定模型
    # 什么时候愿意用它。

    # TODO 3（你写）：发起一次异步调用
    # 用 [HumanMessage(content=message)] 作为消息列表，await bound_model.ainvoke(...)
    ai_message: AIMessage =await bound_model.ainvoke([HumanMessage(content=message)]) # type: ignore[assignment]
    messages:list=[HumanMessage(content=message),ai_message]
    tool_messages:list[ToolMessage]=[]
    def add(first_number:float,second_number:float)->float:
        return first_number+second_number
    # TODO 4（你写）：打印原始字段，逐项核对
    # 必须能看到：
    #   1. AIMessage.content（模型附带说的话，可能为空）
    #   2. ai_message.tool_calls 整体
    #   3. 每个 tool_call 的 id / name / args 三个字段单独打印
    # 注意：add 的真正执行发生在下方"先校验、后执行"的 try 块里，校验失败则不会执行
    print(f"[!] 模型返回的原始字段：\n{ai_message.content}")
    print(f"[!] 模型返回的 tool_calls 整体：\n{ai_message.tool_calls}")
    for tool_call in ai_message.tool_calls:
        print(f"[!] tool_call.id  : {tool_call['id']}")
        print(f"[!] tool_call.name: {tool_call['name']}")
        print(f"[!] tool_call.args: {tool_call['args']}")

        # Runtime 的信任边界：模型返回的 args 只是"提议"，先校验、后执行，
        # 顺序不能反——校验失败时 add 永远不会被调用。
        try:
            validated = AddArgs(**tool_call["args"])
            result = add(**validated.model_dump())
            print(f"[!] args 通过 schema 校验，add 执行结果: {result}")
            # 配对凭证落地：tool_call_id 必须和发起请求的 tool_call["id"] 一模一样
            tool_messages.append(ToolMessage(content=str(result), tool_call_id=tool_call["id"]))
        except ValidationError as e:
            print(f"[!] args 不符合 schema（模型偏离了参数名！），已拦截、未执行:\n{e}")
            # 失败也要回填：用同一个 id 把错误告诉模型，让它有机会自我纠错、重新发起调用
            tool_messages.append(ToolMessage(
                content=f"参数校验失败，请使用正确的参数名 first_number 和 second_number 重新调用: {e}",
                tool_call_id=tool_call["id"],
            ))

    # 有回填才有第二轮：没有任何 ToolMessage 时（比如模型压根没选工具），不发无意义的第二次调用
    if tool_messages:
        messages.extend(tool_messages)
        final_messages:AIMessage=await bound_model.ainvoke(messages)
        print(f"\n[!] 第二轮消息链（共 {len(messages)} 条）:")
        for m in messages:
            print(f"    - {type(m).__name__}: {m.content!r}")
        print(f"[!] 模型最终回答: {final_messages.content}")
    else:
        print("\n[!] 没有可回填的 ToolMessage，跳过第二轮调用")
    return ai_message


def main() -> None:
    result = asyncio.run(run("计算 123 + 456"))
    if not result.tool_calls:
        print("\n[!] 模型没有选择工具——检查 schema 描述或调整提示词后重试")


if __name__ == "__main__":
    main()