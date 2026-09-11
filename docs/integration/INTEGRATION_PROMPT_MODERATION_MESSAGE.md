# 集成提示词：data_inspection_failed 可读失败消息（feat/backend `41cc5de`）

## 给 Git Integrator 的一句话

`feat/backend` 新增一个 commit（`41cc5de`）：provider 内容审查拒绝（阿里云
`data_inspection_failed`）时，失败事件从裸类型名 `BadRequestError` 升级为
已分类 reason + 固定可读文案。纯加性改动，无契约破坏，可直接合入。

## 背景（为什么）

用户会话 `a681b07a` 失败，UI 只显示 `model call failed: BadRequestError`，
排障要翻服务端日志才看到真因：阿里云百炼专有端点对含检索网页文本的二次
模型调用返回 400 `data_inspection_failed`（内容安全审查拒绝——维基百科
「戒急用忍」词条涉敏感词面）。

诊断结论（已与用户确认）：

- 会话失败与 Langfuse 无关。Langfuse 无记录是旁路熔断丢写（服务进程网络
  环境连不上 `jp.cloud.langfuse.com`，设计内降级），两件事恰好同时发生。
- fallback 不接管是策略正确（`TwoLevelFallbackPolicy` 对 4xx 不切换）。
- 本次修复只做「失败消息可读化」，不改 fallback 策略（用户明确不选重试
  方案——内容已判罚，重试无意义）。

## 改了什么

| 文件 | 改动 |
| --- | --- |
| `src/agent_harness/agent/runtime.py` | 新增 `_classify_provider_failure()` + 常量 `CONTENT_MODERATION_REASON`/`CONTENT_MODERATION_MESSAGE`；异常臂分类后向 `model/failed` 传 `readable_message`、向 `run/failed` 传 `reason`+`message` |
| `src/agent_harness/session/session.py` | `Session.end_run` 增加 failed 语义的 `message: str \| None = None` 参数（缺省不落键） |
| `tests/agent/test_runtime_failure_paths.py` | +4 测试：run/run_stream 双路径分类断言、model/failed 可读+脱敏断言（provider 回显原文不得进任何事件）、未分类错误回归锁 |
| `tests/agent/test_run_finalizer.py` | +2 测试：`append_model_failed` readable_message 覆盖、`failure_terminal` reason+message 成对落盘 |

## 契约变化（加性）

- `run/failed.data.reason` 新增枚举值 `"provider_content_moderation"`
  （既有：`cancelled` / `orphaned` / `identical_tool_failure_loop` /
  上下文超限；合同文档本就是非穷举枚举，与 `identical_tool_failure_loop`
  同模式，故未改合同文档）。
- `run/failed.data.message` / `model/failed.data.message` 新增已分类故障的
  固定可读文案（中文）。前端 `runState.ts` 只特判 `reason === 'cancelled'`，
  未知 reason 自然落入 failed 分支——**无前端破坏**。
- 后续可选前端票：事件检查器/会话列表把该 message 直接渲染出来（本次只
  做后端，前端未动）。

## 不变量守护（review 已确认）

- 脱敏不变量不松动：事件只带本项目常量，provider 回显原文仍只进结构化
  日志（`_log(exc_info=True)`）。测试断言 `inappropriate` /
  `chatcmpl-*` 不出现在任何持久化事件 data。
- 未分类错误字节级行为不变（类型名消息、run/failed 不落 reason/message 键，
  有回归锁）。
- 取消臂不受影响（`readable_message` 默认 None）。

## 门禁证据

- `ruff check src/ tests/`：All checks passed。
- 全量 pytest：**1612 passed / 10 skipped / 0 failed**（基线 1606 + 新增 6）。
- commit：`41cc5de`（feat/backend，未 push，§16.4 由集成 AI 处理）。
