# W-26 · 进度清单服务端契约（schema · 整表覆盖 · 硬校验）
**目标仓库**：intelligence-agent-backend（Python Core / SessionEvent 投影）。

**类型/优先级**：P0 Contract。**依赖**：W-02（#346 保护事实，清单是其压缩锚点之一）。**范围**：`session/event.py`、`session/derive.py`、新增 `session/plan.py`（或既有投影模块就近）、工具注册（ToolExecutor 统一路径）与对应测试。实施前重读 Engineering Spec 03（SessionEvent）、PRD `docs/PRD_LONG_TASK_CONTEXT_MANAGEMENT.md` §7。

## 明确契约

事件类型 `task/plan_updated`（append-only，不变量 #3）。payload 为**全量清单**（整表覆盖）：

```json
{"items": [{"id": "str", "content": "str", "activeForm": "str",
            "status": "pending|in_progress|completed",
            "source": "initializer|user|agent"}]}
```

状态机：`pending↔in_progress`、`in_progress→completed`；completed 不可回退（回退=新增项）。**handler 硬校验**（任一违反 → 拒绝整个更新、不产生事件、返回完整当前清单+错误原因）：

1. `in_progress` 恰为 0 或 1 项（单 in_progress）；
2. 条数 ≤ 50（软上限；超过返回错误「要求合并相邻项」——无产品先例，工程判断，W-30 后按实测调整）；
3. status/source 枚举合法；id 在表内唯一。

工具契约：`update_plan`（PORT DESIGN 自 Codex `update_plan` / Gemini `write_todos`），每次提交全量清单；走 ToolExecutor 统一执行路径（不变量 #7），不经旁路。投影：客户端从 SessionEvent 重放得到当前清单（不变量 #22，不维护第二套真相）。

## 工作指令

1. 先写判据测试：双 in_progress 拒绝、51 条拒绝、completed 回退拒绝、合法整表接受且可重放重建、缺字段/非法枚举拒绝且**不产生事件**（读 store 验证）。
2. 事件 schema 校验失败时错误文案必须含：违反的不变量编号 + 当前完整清单（供弱模型自修复）。
3. 投影必须是纯函数（events → plan state），Fork 按 #346 同边界继承那一刻的清单。

## 验收

- 上述判据测试全绿；投影重放 N 次结果一致（幂等）；事件 append-only 不改写旧事件。
- 运行 focused session 测试、ruff，按 V3.1-lite 记录冻结树与 review。

**不做**：依赖图（blocks/blockedBy，Claude Code 式增量补丁不引入）、渲染（W-27/W-28）、压缩集成（W-29）、清单条数硬上限以外的配额体系。

**成熟参考/复用**：三态状态机 + 单 in_progress + 渲染四件套为全行业收敛（zcode 实测 UI、Cline、Claude Code，见 `docs/research/2026-09-27-agent-progress-visualization-research.md`）；整表覆盖选 Codex/Gemini 范式（弱模型不易写坏、天然解决并发合并）；Claude Code 增量补丁范式（唯一带依赖图）明确不引入。`activeForm` 字段名沿用 Claude Code `TaskCreate` 同款。
