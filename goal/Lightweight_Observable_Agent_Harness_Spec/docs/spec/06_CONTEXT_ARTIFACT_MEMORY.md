# 06 — Context + Artifact + Memory

## 1. 三个基础概念

### Persistent History
完整事实记录，不能因模型窗口不足而删除。

### Runtime Context
某一次 Model Call 实际发送给模型的信息。

### Artifact
完整保存、但默认不直接注入模型的大对象/大输出。

核心原则：

> **完整保存 ≠ 完整注入。**

## 2. Context Builder

建议组合：

```text
System
+ active AgentProfile
+ selected Skills
+ structured session summary
+ recent complete turns
+ relevant artifact refs
+ Knowledge evidence
+ Memory Context Provider output
+ task-specific refs
```

禁止简单：

```python
messages = messages[-20:]
```

因为可能丢用户约束、Tool Result、恢复信息、历史决策。

## 3. Artifact Store

大输出流程：

```text
Raw Tool Output / Large File / Dataset
→ ArtifactStore.save()
→ Local or MinIO Provider
→ metadata/hash/type/size/ref
→ compact summary + artifact_ref
→ Model Context
```

默认 Provider：
- Local filesystem：开发/小型部署；
- MinIO：正式大文件、大量对象。

接口 SHOULD 保持对象存储抽象，未来可换 S3。

## 4. inspect_artifact

作为 READ_ONLY Tool：

```text
artifact_ref
start/end lines?
keyword?
range?
query?
```

模型按需重新读取局部细节。

大 Artifact 不允许每次完整灌回 Context。

## 5. Compaction

```text
early complete turns
→ preserve tool interaction boundaries
→ structured summary
→ replace only runtime projection
→ persistent SessionEvent unchanged
```

Summary 至少保留：
- facts
- decisions
- constraints
- failed_attempts
- unresolved
- artifact_refs
- citations
- important tool outcomes

不能拆断 AI tool_call 与对应 ToolResult。

默认配置：
- `auto_compact_threshold = 0.70`
- `hard_guard_threshold = 0.85`

必须可配置。

Summary LLM 失败时应有 deterministic fallback；hard guard 下 compaction 仍失败则阻止继续无脑撑爆窗口。

## 6. Memory：Capability / Context Provider

### 冻结决定

Memory MUST 暴露成：

```text
MemoryCapability
     +
MemoryContextProvider
```

Core 不直接依赖 LangMem、Mem0 或自研实现。

### Provider

当前默认：

```text
MemoryProvider
├─ MarkdownMemoryStore  # 当前默认：MEMORY.md / USER.md 文件内容权威
├─ LangMemProvider      # 可选 Adapter（V1 历史默认，其默认地位被 ADR-0056 §7 取代）
├─ Mem0Provider         # future
└─ CustomProvider       # future
```

当前默认实现是仓内 Markdown 文件存储（`memory/v2/markdown_store.py`）：`MEMORY.md` 承载
collection 内容，`USER.md` 承载 profile 内容。内容的**唯一可变事实入口**是统一提交 seam
（`MemoryCommit`，#1494）；Tool、service、formation、审批与批量删除都只向它交意图，
不得自带第二套写序（ADR-0056 §3.1）。检索索引与 outbox 是可重建派生态，不拥有任何当前事实
（ADR-0056 §3.8）。

建议抽象能力（供非文件 Provider 参考，不要求当前文件实现逐一对齐）：

```text
extract_candidates()
store()
update()
delete()
recall()
search()
```

但 Core Runtime 不要求所有 Provider 内部算法一致。

### Context Provider

模型调用前：

```text
task/session/user context
→ MemoryContextProvider.select(...)
→ compact memory entries
→ ContextBuilder
```

Memory Context Provider 应负责：
- 当前文件内容的选取（不按身份/项目路由过滤：裁决 7）
- relevance
- recency
- importance
- token budget
- citation/reference metadata（如适用）

### Memory Tool

若需要让模型显式操作 Memory，可额外暴露 Tool，但这不是唯一入口。自动 recall 更适合作为 Context Provider。

## 7. Memory Scope

Memory 是**本机单人文件资产**，不再按身份路由。当前合同：

- 内容按目标分文件落盘：`USER.md`（profile，稳定用户偏好/画像）与 `MEMORY.md`（collection，
  semantic/episodic/procedural 三类内容），由统一提交 seam 写入（ADR-0056 §3.1）。
- 不按 tenant / user / project 做记忆路由（裁决 7 整体退役）。旧 `user_global` / `project`
  两 scope 与其隔离/可见性规则**不再是当前合同**；旧记录的三元身份只作为只读迁移来源标签
  保留（ADR-0056 §3.7，#1493），不进入运行路径，也禁止换名为 `owner_id` 复制同一套路由。
- `task` 与 `agent` scope 同样不引入；增加任何新的记忆身份层前，必须有单独批准的 ticket
  定义可信身份来源、生命周期、隔离、权限与召回可见性。
- 记忆边界由**读写两侧**共同守住：写前扫描（delimiter / secret / threat）与读盘后逐条扫描，
  以及 pin / revision 校验，都是 Runtime 边界，不由模型选择（ADR-0056 §6）。
- **Project 目录/会话分组不因去记忆 scope 消失**：它是产品组织概念，不是记忆路由
  （ADR-0056 §2）。

Memory 的存储实现不应改变 Agent Core。

## 8. Failure Semantics

- Memory Provider 不可用：Core SHOULD graceful degrade，不阻塞基础 Agent。
- 记忆写入失败：必须如实报错，不能把部分成功伪装成全成功。提交 seam 的不确定结果一律进入
  NEED_RECONCILE 交人工/恢复对账，不盲重跑（ADR-0056 §3.6）。
- 记忆写入成功：必须留下可核验的提交回执（operation id + 每目标 previous/new digest）；
  回执与恢复状态机不拥有内容事实，只在重启后判定「未发布 / 已发布 / 冲突」。
- 删除抑制：删除提交后，墓碑继续拒绝旧输入与延迟作业重放；墓碑记录删除理由、内容/来源 hash
  与过期策略，不保留被删内容（ADR-0056 §3.5）。
- 派生索引（FTS / 进程内 index / outbox）失败或丢失：只能由当前已批准的 Markdown 重建，
  不得反向成为事实来源（ADR-0056 §3.8）。
- Artifact Store 上传失败：不能伪造 artifact_ref。
- Summary 失败：保存 raw artifact，走 fallback summary。
- Compaction 拆断 tool pair：拒绝结果。
- Context hard guard：必须停止或要求用户处理，不能继续发送超窗口请求。

## 9. Acceptance Criteria

- 大 Tool Output 完整保存；
- Model 默认只收到 summary + ref；
- `inspect_artifact` 能找回细节；
- MinIO Provider 可替换 Local Provider；
- Compaction 后历史不删除；
- Memory 默认实现（Markdown 文件）可正常 recall 与注入；
- 替换成 Fake/Mem0/LangMem Provider 时 Agent Core 无需修改；
- Memory Provider 故障时基础 Agent 仍能工作；
- 当前内容唯一可变事实在 Markdown 文件，且不存在第二内容写者（#1494 AC5）；
- SQLite 独有内容不被当前检索返回（#1495 AC1）；
- 清空可重建检索后，同集合/同 revision 可恢复（#1495 AC4）；
- 墓碑重放继续被拒绝；启动/提取/审批/治理/重启均不创建 Memory DB（#1496 AC1/AC3）。
