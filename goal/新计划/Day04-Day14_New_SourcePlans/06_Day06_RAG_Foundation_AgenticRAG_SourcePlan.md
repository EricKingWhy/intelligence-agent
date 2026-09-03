# Day 06 Source Plan — RAG Foundation + Agentic RAG

> **主要受众：Codex（Daily Curriculum Author）**  
> 本文件不是 Claude Code 的最终授课讲义，也不是一次性施工清单。  
> Codex 必须结合 `Agent-0to1-Module-Roadmap.md`、`Agent-0to1-Learning-Workflow.md`、当前代码状态与前一日完成情况，生成真正的 `Day06-Learning-Plan.md`。  
> **Module 是硬边界，Day 是软时间盒。** 若本日核心未完成，下一学习日继续；若提前完成，可在用户确认后进入下一个 Module。

- **Primary Module(s)：** Module 6 + Module 7
- **建议时间：** 约 4～5 小时；若未完成可自然延续
- **总方向：** AI 应用开发优先；核心机制细拆但不深挖；外围工程允许 AI Coding 主导但不得黑盒。
- **默认执行：** 一次只允许一个 ACTIVE Task。

---

# 1. 今天为什么合并两个旧阶段

旧课程把：

```text
Markdown Parser / Chunker
→ Embedding / Milvus
→ Agentic RAG / Incremental Index / Citation
```

拆成三天。

新版保留完整链路，但学习策略改变：

- **Chunk 的最小原理亲手做；**
- **Parser、Embedding、Milvus plumbing 大量 AI Coding；**
- **Agentic RAG 再回到 CORE_LEARNING。**

所以可以在一个较长学习日内完成，未完成则延续，不允许为了日期硬赶。

# 2. 今天最终工程主链

```text
Markdown
→ Section / Metadata
→ 极简 Token Chunker
→ EmbeddingProvider
→ Milvus
→ persistent knowledge

然后

Agent Loop
→ model decides
→ retrieve_knowledge Tool
→ Milvus search
→ sufficient + Citation
→ ToolResult
→ LLM
```

# 3. 今天必须亲手完成

1. 亲手实现一个**极简 token-window Chunker**：
   `text → token → window → overlap → chunk`。
2. 手算一次 `chunk_size / overlap / step`。
3. 真正 ingest 至少两个 Markdown 文档到 Milvus。
4. 重启 Python 后直接 search，确认知识不是进程内变量。
5. 注册 `retrieve_knowledge` Tool。
6. 做三种 Agentic 行为：
   - 该搜；
   - 不该搜；
   - 没有证据。
7. 最终答案实际使用知识时看到真实 Citation。

# 4. CORE_LEARNING：极简 Chunker

真正要懂：

```text
tokens = encode(text)
step = chunk_size - overlap

window 1
window 2
...
```

必须回答：

- 为什么按 token 而不是纯字符更接近模型 Context；
- overlap 为什么能减少跨边界信息丢失；
- `overlap >= chunk_size` 为什么会导致 step<=0 / 死循环风险；
- 最后一段不足 chunk_size 为什么也要保留。

不要求自己手写复杂 Markdown Parser。

# 5. Markdown Heading：理解设计，不钻 Parser

保留：

```text
heading_path + content
→ embedding_text
```

Milvus 中仍应保留原正文与 metadata。

用户只需理解：

> Heading 是检索上下文的一部分，不只是 UI 展示字段。

复杂内容：

- fenced code block；
- H1/H2/H3 parser 边界；
- Markdown AST 细节；

交成熟库或 AI Coding。

# 6. AI_CODING_PRACTICE：Embedding + Milvus

Claude 主导：

- EmbeddingProvider；
- batch embedding；
- API 配置；
- Milvus collection；
- metadata schema；
- ingestion service/CLI；
- integration test plumbing。

用户重点看：

```text
Chunk
→ embedding_text
→ vector
→ vector + metadata insert
→ query vector
→ search
```

Milvus 只掌握应用层：

```text
insert
search
filter
delete
persistence
```

不学集群运维和复杂索引调优。

# 7. Ingestion 与 Runtime 分离

必须理解：

```text
agent kb ingest
≠
agent chat
```

Agent 每次启动不能：

```text
scan all docs
→ re-embed
→ insert again
```

原因：

- 慢；
- 浪费 API；
- 重复数据；
- Runtime 与数据维护耦合。

# 8. Agentic RAG 核心

Knowledge 不再是固定 Pipeline：

```text
User
→ 强制 retrieval
→ generation
```

而是：

```text
retrieve_knowledge
= READ_ONLY Tool

Model 根据任务决定是否调用
```

不要写：

```python
if "知识库" in user_input:
    retrieve()
```

# 9. retrieve_knowledge Tool

至少包含：

```text
query
kb_id
top_k
```

返回保留：

```text
chunk_id
doc_id
file_name/source
heading_path
score
content
```

以及：

```text
sufficient
```

Tool Description 必须说明：

- 什么时候应该用；
- 什么时候不要用；
- 没有足够证据要明确返回不足。

# 10. Citation

核心原则：

> Citation 必须从 Retrieval Result 开始一直携带，不能最后让模型凭空补来源。

正确：

```text
Milvus
→ result + source metadata
→ ToolResult
→ ToolMessage
→ LLM
→ final citation
```

如果中途只留下 content，最后无法可靠恢复来源。

# 11. Incremental Index

保留应用层理解：

```text
scan file
→ source exists?
→ mtime changed?
→ hash changed?
→ unchanged = skip
→ changed = reindex
```

用户必须懂：

- unchanged 为什么不重复 embedding；
- `doc_id` 为什么作为稳定文档身份；
- V1 `delete old → insert new` 有窗口风险。

AI Coding 主导：

- mtime/hash 工程细节；
- metadata status；
- CRUD；
- failure update。

# 12. V2 Atomic Update

今天只记设计债务：

```text
new version write
→ verify
→ switch active_version
→ delete old
```

不要求今天实现。

# 13. Failure / Debug

至少：

### A. `overlap >= chunk_size`
配置直接拒绝。

### B. Embedding API 失败
metadata 不能显示 INDEXED。

### C. Knowledge 没相关内容
` sufficient=false `，Agent 不得把预训练知识伪装成检索证据。

### D. Milvus 暂停
Knowledge Tool 返回明确错误，ToolExecutor 依据策略处理。

# 14. 推荐 Task 粒度

可参考：

```text
Task A — 极简 Chunker（CORE）
Task B — Embedding + Milvus Ingestion（AI Coding）
Task C — Persistent Search + Metadata（AI Coding + Hands-on）
Task D — retrieve_knowledge Tool + Agentic Behavior（CORE）
Task E — Incremental Index + Citation + Failure E2E（混合）
```

# 15. Scope Lock

不做：

- PDF / Word / OCR；
- Hybrid BM25；
- Rerank；
- Web Search；
- CRAG；
- Multi-query；
- 复杂原子版本切换；
- Milvus 集群调优。

# 16. 完成 Gate

- [ ] 用户亲手写过极简 Chunker；
- [ ] 能口述 Markdown→Chunk→Embedding→Milvus；
- [ ] 程序重启后知识仍可检索；
- [ ] Knowledge 成为正式 READ_ONLY Tool；
- [ ] Agent 会自己决定搜/不搜；
- [ ] 无证据时不瞎编；
- [ ] Citation 全链保留；
- [ ] unchanged 文档不会重复 embedding。
