# Day 07 Source Plan — Session / Checkpoint / Operation Ledger / Crash Recovery

> **主要受众：Codex（Daily Curriculum Author）**  
> 本文件不是 Claude Code 的最终授课讲义，也不是一次性施工清单。  
> Codex 必须结合 `Agent-0to1-Module-Roadmap.md`、`Agent-0to1-Learning-Workflow.md`、当前代码状态与前一日完成情况，生成真正的 `Day07-Learning-Plan.md`。  
> **Module 是硬边界，Day 是软时间盒。** 若本日核心未完成，下一学习日继续；若提前完成，可在用户确认后进入下一个 Module。

- **Primary Module(s)：** Module 8 — Recovery
- **建议时间：** 约 4 小时；S+，不允许为了进度压缩
- **总方向：** AI 应用开发优先；核心机制细拆但不深挖；外围工程允许 AI Coding 主导但不得黑盒。
- **默认执行：** 一次只允许一个 ACTIVE Task。

---

# 1. 今天是整个项目的 S+ 核心日

今天重点不是“把 messages 存数据库”，而是理解：

> **状态恢复（State Recovery）和外部副作用恢复（Side-effect Recovery）是两件不同的事。**

如果今天没有真正理解，不进入 Context 模块。

# 2. 主状态模型

必须区分：

```text
Session
= 长期任务/对话容器

Run
= 一次 user request 驱动的一轮 Agent 执行

Checkpoint
= 已经持久化成功、可以恢复的稳定状态事实

Tool Operation
= 某次真实外部操作执行到了哪里
```

ID 至少：

```text
session_id
run_id
checkpoint_id
operation_id
tool_call_id
```

# 3. 今天必须亲手完成

1. 亲手画 Session / Run / Checkpoint / Operation 四层关系。
2. 看懂并参与确定 Checkpoint 保存边界。
3. 故意制造：
   `Tool 实际执行成功 → 进程 crash → ToolMessage 尚未保存`。
4. 重启后做 reconcile，而不是盲重跑。
5. 对一个 UNKNOWN Bash 做人工/保守恢复判断。
6. 从日志/数据库找到 operation 状态与原 `tool_call_id`。

# 4. Checkpoint 核心

建议稳定边界：

```text
USER_ACCEPTED
MODEL_COMPLETED
TOOL_BATCH_COMPLETED
FINAL_COMPLETED
```

原则：

> Checkpoint 代表“可恢复的事实”，不是“程序代码跑到这一行”。

例如 AIMessage 还没有写入 messages，就不能先把 MODEL_COMPLETED 标为完成。

# 5. Transaction

同一稳定边界尽量事务化：

```text
insert message
+
checkpoint update/insert
```

避免：

```text
message 有
checkpoint 没有
```

或反过来。

SQLite transaction 代码可 AI Coding，但事务边界必须用户真正懂。

# 6. Operation Ledger

推荐状态：

```text
PENDING
→ RUNNING
→ SUCCEEDED / FAILED / CANCELLED
```

Crash 后：

```text
RUNNING
→ UNKNOWN / NEED_RECONCILE
```

必须理解：

> RUNNING 只能证明“启动过”，不能证明外部世界到底发生了什么。

# 7. 为什么只有 Checkpoint 不够

关键场景：

```text
bash("python migrate.py")
→ migration 实际成功
→ Python 进程 crash
→ ToolMessage 未写
```

只根据前一个 Checkpoint 重新执行 Bash 可能导致：

- 重复迁移；
- 重复支付；
- 重复删除；
- 重复部署。

所以必须有 Operation Ledger / idempotency / reconcile 思维。

# 8. Reconcile 基础策略

## SUCCEEDED + ToolMessage 缺失

```text
operation.result_json
→ Recovery ToolMessage
→ 使用原 tool_call_id
→ 补回 Message Chain
```

## FAILED
补失败 ToolMessage，让模型继续处理。

## CANCELLED
补明确 CANCELLED Result。

## PENDING
说明真正 Tool 还没启动，可重新执行。

## RUNNING / UNKNOWN
需要 Tool-specific reconcile。

# 9. 四类 Tool 恢复差异

## read
READ_ONLY，通常可安全重读。

## retrieve_knowledge
READ_ONLY，通常可安全重新查询。

## write/edit
可做状态验证：

```text
目标内容是不是已经写成预期？
```

确认成功则补 SUCCEEDED；不确定则不要盲目执行。

## bash
最危险。

```text
pytest
pip install
curl POST
migration
```

语义完全不同。

V1 对 UNKNOWN Bash 默认保守，不假装能自动判断所有副作用。

# 10. Message Chain Consistency

恢复后还必须检查：

```text
AIMessage.tool_calls
↔
ToolMessage(tool_call_id)
```

不能留下 dangling Tool Call。

Recovery ToolMessage 仍使用原 `tool_call_id`。

# 11. AI_CODING_PRACTICE 可主导

- SQLite table DDL；
- repository/DAO；
- CRUD；
- serialization；
- status query CLI；
- history 输出；
- 一般单元测试样板。

但以下不能黑盒：

- 状态模型；
- checkpoint boundary；
- transaction boundary；
- operation lifecycle；
- reconcile 规则；
- crash gap。

# 12. 必做 Crash Cases

至少覆盖：

### Crash A
Tool SUCCEEDED，ToolMessage 未保存。

### Crash B
Bash 是 RUNNING，进程被 Kill。

### Crash C
read 仅 PENDING，尚未真正执行。

### Crash D
ToolMessage 已完整存在，恢复时不能重复补。

# 13. 真 Kill Experiment

必须真实杀一次 Python 进程，而不是只写 mock。

流程：

```text
Run
→ Tool operation
→ kill
→ restart
→ load latest completed checkpoint
→ inspect operation
→ reconcile
→ restore message chain
→ continue
```

# 14. Scope Lock

不做：

- LangGraph Checkpointer；
- Distributed transaction；
- Redis；
- Kafka；
- exactly-once 神话；
- 通用 Bash 自动补偿引擎；
- Multi-Agent Recovery。

# 15. 完成 Gate

用户必须能脱离代码回答：

- Session / Run / Checkpoint / Operation 的区别；
- Checkpoint 为什么不是“执行到哪”；
- RUNNING 为什么危险；
- Tool 成功但 ToolMessage 丢了怎么办；
- 为什么 write/edit/bash 恢复策略不同；
- 为什么 idempotency/reconcile 是 Agent Recovery 核心。

并完成真实 Kill / Resume。
