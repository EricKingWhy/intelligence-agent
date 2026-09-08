# 集成提示词：B2 冲突解决 → B2-MixIn 集成入 main + feat/backend 清理

> **给 Git Integrator。** B2（context_providers 运行时消费）的平行实现冲突已解决，
> 需要你执行 push + 集成决策。本文档是你的完整任务上下文。

---

## 背景：发生了什么

B2 有两个独立实现碰了同一批文件：

| 实现 | 分支 | Commit | 方案 | ADR |
| --- | --- | --- | --- | --- |
| **A** | `feat/runtime-context-providers`（已在 main `e18b7fa`） | `0e3f037` | 类属性 `name` + fail-open | ADR-0020b |
| **B** | `feat/backend` | `a731563` | ContextProviderEntry dict + handler 422 | ADR-0021 |

集成 AI 已分析并批准**混合方案**（Ticket B2-MixIn）：保留 main 的实现 A 结构不动，
只从实现 B 借鉴 handler 层 422 校验这一个核心优势。

## 已完成的 B2-MixIn 实现

- **分支**：`feat/runtime-context-providers-422`
- **Worktree**：`D:\intelligence-agent-runtime`
- **起点**：`origin/main`（`080e1f1`）
- **Commits**：
  - `c3b92ae` — handler 层 422 校验（`web/app.py` +21 行，测试 +63 行）
  - `d67d8e8` — 集成交接单
- **状态**：未 push（等你执行），未 merge
- **交接单**：`D:\intelligence-agent-runtime\docs\integration\BACKEND_B2_MIXIN_HANDOFF.md`

### 改了什么

只改 `src/agent_harness/web/app.py` 的 `create_session` handler——在 `get_wiring()`
后、`build_runtime` 前加集合校验：未知 `context_providers` id → 422 + detail 含
available 清单。没碰 wiring / provider 类属性 / assembly / Pydantic validator。

### 测试

- web + assembly 相关：73 passed（含 5 条新增）
- ruff clean
- 55 预存失败（Python 3.11 `Path.read_text(newline=)` + 缺 langfuse）与本次改动无关

---

## 你需要做什么

### 第一步：push（用户已授权你执行）

```bash
# 在 D:\intelligence-agent-runtime worktree
git push origin feat/runtime-context-providers-422
```

### 第二步：集成 B2-MixIn 入 main

按标准集成流程：

```bash
# D:\intelligence-agent（main worktree）
git fetch origin --prune
git checkout main
git merge origin/feat/runtime-context-providers-422
# 验证 gate（pytest + ruff）
# commit phase-status
```

集成风险**极低**：21 行生产代码，不碰实现 A 的任何结构，与 main 现有测试全兼容。

### 第三步：处理 feat/backend 的 superseded 部分

`feat/backend`（`a731563`，已 push 到 origin）包含：

- ✅ **T5 #135**（`1b3179d`）—— MinIO 大产物外置 + fail-open overflow —— **仍有效**，需要集成
- ❌ **B2 / ADR-0021**（`a731563`）—— ContextProviderEntry 平行实现 —— **已被 B2-MixIn 取代**

集成 `feat/backend` 时，按 §14.7 逐文件分析：

- `assembly.py` / `web/app.py` 的 context_providers 改动 → **丢弃**（采纳 main 的实现 A + B2-MixIn）
- `capability/wiring.py` 的 `ContextProviderEntry` / `register_context_provider` → **丢弃**
- T5 相关文件（`storage/minio_artifact.py` / `tools/read_artifact.py` / `overflow.py` / `event.py` / `config.py`）→ **保留**
- 测试文件 `test_assembly_context_providers.py` → **会有冲突**（同名但不同实现），保留 main 版本
- 测试文件 `test_web_context_providers_b2.py` → **丢弃**（功能已被 B2-MixIn 的测试覆盖）

建议：先单独集成 B2-MixIn 入 main，稳定后再处理 `feat/backend`（届时 `feat/backend` 的
context_providers 部分会被 main 的版本覆盖，T5 部分正常合并）。

### 第四步：更新 PHASE_STATUS.md

记录 B2-MixIn 集成 + 标注 `feat/backend` 的 B2 部分被取代。

---

## 参考文档

- `D:\intelligence-agent\docs\integration\TICKET_B2_MIXIN_HANDLER_422.md` — 原 ticket（集成 AI 写的混合方案）
- `D:\intelligence-agent-runtime\docs\integration\BACKEND_B2_MIXIN_HANDOFF.md` — 本次实现的完整交接单
- `docs/adr/0020b-context-providers-runtime-consumption.md`（main）— 实现 A 的 ADR
- `docs/adr/0021-context-providers-runtime-consumption.md`（feat/backend）— 实现 B 的 ADR（已被取代）

## 铁律

- §14.4：merge / push 需用户批准 —— **用户已授权你执行 push 和集成**
- §14.6：先回后正（feature 先 merge main，稳定后再入 main）
- §14.7：冲突逐文件分析，不机械 ours/theirs
- §14.9：一次一个分支（先 B2-MixIn，再 feat/backend）
- 零密钥泄露（.env 内容绝不打印/提交/复制进文档）
