# Day 05 Source Plan — Docker Sandbox + read/write/edit/bash

> **主要受众：Codex（Daily Curriculum Author）**  
> 本文件不是 Claude Code 的最终授课讲义，也不是一次性施工清单。  
> Codex 必须结合 `Agent-0to1-Module-Roadmap.md`、`Agent-0to1-Learning-Workflow.md`、当前代码状态与前一日完成情况，生成真正的 `Day05-Learning-Plan.md`。  
> **Module 是硬边界，Day 是软时间盒。** 若本日核心未完成，下一学习日继续；若提前完成，可在用户确认后进入下一个 Module。

- **Primary Module(s)：** Module 5 — Sandbox + Coding Tools
- **建议时间：** 约 3～4 小时
- **总方向：** AI 应用开发优先；核心机制细拆但不深挖；外围工程允许 AI Coding 主导但不得黑盒。
- **默认执行：** 一次只允许一个 ACTIVE Task。

---

# 1. 今天真正要获得的工程能力

让 Agent 可以在**隔离的工作空间**中真正完成 Coding 任务：

```text
Agent Loop
→ ToolExecutor
→ read / write / edit / bash
→ DockerSandbox
→ /workspace
```

目标不是学 Docker 底层，而是掌握 AI Coding Agent 中：

> **工具执行为什么必须有 Runtime 安全边界，以及 Tool / Sandbox 如何分层。**

# 2. Learning Mode

总体：

```text
核心边界理解：CORE_LEARNING（较轻）
Docker plumbing：AI_CODING_PRACTICE
Coding Tool 语义：A 级应用工程
```

不要让用户手写大量 `docker exec/cp/inspect` plumbing。

# 3. 今天必须亲手完成

1. 亲手确认 Host / Container / Workspace 的边界。
2. 用 Agent 对 Toy Project 完成一次真实：
   `read → edit → bash pytest → 根据失败继续修改 → pass`。
3. 故意制造一次 `edit` 多处匹配，观察 Tool 拒绝而不是静默修改第一处。
4. 故意让一次 `pytest` 返回 exit code 1，确认这不是“Tool Runtime 网络异常”。
5. 从 JSONL 定位一次 bash 调用失败。

# 4. 必须理解的核心

## 4.1 Sandbox 是 Runtime，不是 Prompt

错误：

```text
System Prompt:
“不要访问用户其他目录”
```

这不是安全边界。

正确：

```text
Tool
→ Sandbox abstraction
→ Container
→ /workspace
```

模型即使尝试访问宿主机敏感路径，Tool 也不应拥有那个执行环境。

## 4.2 Host / Container / Workspace

只需应用层理解：

- Host：用户真实机器；
- Container：隔离运行环境；
- Workspace：Agent 在容器内允许操作的项目目录。

不学：
- namespace；
- cgroup；
- container runtime 内核。

## 4.3 Sandbox Contract

保持小：

```text
ensure_started
exec
read_text
write_text
copy_in
stop
```

Tool 逻辑不要写进 Sandbox。

## 4.4 Workspace 导入

优先安全策略：

```text
宿主机项目
→ 显式 copy/import
→ Docker volume
→ /workspace
```

不要默认直接把用户真实项目目录高风险 bind mount 给 Agent 任意修改。

## 4.5 read / write / edit / bash

### read
- READ_ONLY；
- 读取 workspace 内文本。

### write
- MUTATING；
- 明确是覆盖写。

### edit
V1 使用：

```text
exact old_string
→ new_string
```

要求：
- 0 match → 明确失败；
- 1 match → 修改；
- >1 match → AMBIGUOUS，拒绝。

### bash
- 默认 MUTATING；
- 返回：
  `exit_code/stdout/stderr/duration`。

必须理解：

> `bash` 工具成功执行，不等于 shell 命令业务成功。

例如：

```text
pytest exit_code=1
```

Tool 调用本身可能成功，Agent 应读取 stdout/stderr 决定下一步，而不是 Executor 自动把它当 transient error 重试。

# 5. AI Coding 主导

Claude 可主导：

- Dockerfile；
- `DockerSandbox` 实现；
- subprocess plumbing；
- container/volume 生命周期；
- Docker unavailable/error mapping；
- 集成测试样板；
- read/write/edit/bash 大部分样板代码。

完成后必须给 Key Diff Walkthrough，而不是逐行带读。

# 6. 用户重点看哪 20%

Codex 应要求 Claude 重点带用户看：

1. Tool 调到 Sandbox 的接口位置；
2. workspace path 如何被约束；
3. bash 的 `ExecResult` / ToolResult 映射；
4. edit 唯一匹配；
5. side_effect 如何设置；
6. timeout 如何从 Tool Runtime 继承。

# 7. Failure / Debug

至少做：

- edit 多匹配；
- pytest 失败；
- Docker/container 暂停或不可用。

观察：

```text
tool_name
tool_call_id
duration
exit_code
error_code
attempt
```

# 8. Scope Lock

今天不要做：

- grep/glob/apply_patch；
- git 专用 Tool；
- Session-scoped Sandbox；
- MCP；
- Artifact Store；
- Context Compaction；
- RAG。

# 9. 完成 Gate

- [ ] Agent 真能修改 Toy Project；
- [ ] 不直接污染 Host；
- [ ] Tool / Sandbox 分层清楚；
- [ ] `bash exit_code=1` 语义清楚；
- [ ] edit 多匹配安全失败；
- [ ] 用户至少亲手完成一次真实 AI Coding 闭环；
- [ ] Docker plumbing 没有变成底层学习课。
