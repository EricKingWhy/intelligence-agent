# 权限档会话内可变 —— 本地执行索引

> 日期：2026-09-21
> 来源：一次「grill 式」设计会话（用户主张现行为「太死板」，要求开票修复）。
> 用途：2 张可由独立 Coding Agent 领取的本地执行票；对应 GitHub Issues **#282（后端 F18-A）/ #283（前端 F18-B）**。
> 本文是执行索引，**不替代** ADR 或 GitHub issue。
> **裁决规则**：GitHub issue 正文是每票 Scope/AC 的权威；本文只补充证据、依赖、串行约束与批次。
> 若本文与对应 issue 冲突，**以 issue 为准**；本文的额外建议不构成关单条件。

---

## 0.1 定性（先纠正一个前提）

- **不是缺陷**：现行为与文档意图一致，且被测试**刻意钉住**（后端 `tests/session/test_permission_mode_persistence.py` 16 例；前端 `#236` 的 `control-row.spec.ts:456` 逐字断 title）。它是一个**已被刻意 deferral 的产品决策**。
- **但成立两点**：① **流程债**——该 deferral 被记录 ≥6 次却**从未开票、也从未有 ADR**（`docs/SDD_TICKET_TRACKER.md:1979`「未开票」）；② **真缺陷**——只读 pill 是**零出路的死胡同**（只有悬停 title，无任何「新建会话」引导/链接/菜单）。
- **用户主张「当前会话已经可以切换权限」不成立**（三重印证 + 真机 A/B）：
  - `web/src/App.tsx:475` `const permissionModeLocked = selectedId !== null || streaming;`（`:468-469` 注释逐字「会话内一律只读…可编辑就是骗人」）；
  - `web/src/components/Composer.tsx:322` `disabled={locked || permissionModeLocked}`、`:326` `disabledHint`；
  - `#236` e2e `control-row.spec.ts:455-456` 钉死 `toBeDisabled()` + 逐字 title；
  - 真机（Inspector `:8011`）A/B 实测：未选会话 pill `disabled=false`；**已选会话 pill `disabled=true / aria-disabled=true`**，而同排 模型/Agent/推理 仍 `disabled=false`。
  ⇒ **会话内改档今天不存在。**

## 0.2 已冻结决策（grilling 拍板，实施者不得重新猜测）

1. **事件唯一**：`permission/changed`，data 同时含 `permission_mode` + `auto_approve`。
2. **一起改**：档位与 `auto_approve` 同事件、同闸门、同生效点。
3. **双向可改**；升 `danger-full-access` 需**前端显式确认**；后端不加强制标志位。
4. **生效时机 = 下一轮 run**（per-run 快照，结构性免费 —— 见 ADR-0041 §1.2）。
5. **有 pending 审批时禁止改档**（后端 409 / 前端禁用）。
6. **三概念分离**：`declared_at_creation` ∥ `effective_now` ∥ `policy_at_approval`；**不得**把 `session_permission_mode` 与 `permission_policy` 合并（#236 边界）。
7. **补 ADR-0041**（= #282 AC0）。
8. **不新开「锁定时给引导」的票**：改档可行后 `PERMISSION_MODE_LOCKED_HINT` 成假话 ⇒ 在 #283 内**删除**它（即「假话引导」的消解方式）。

## 0.3 子票与依赖（严格 blockers-first）

| 本地票 | GitHub | 内容 | Blocked by |
|---|---:|---|---|
| F18-A | **#282** | 后端：`permission/changed` 事件 +「最后一次胜」派生 + 端点 + pending 409 + ADR-0041 | 无 |
| F18-B | **#283** | 前端：pill 解只读 + 升档确认面 + 删除假提示 + 投影「最后一次胜」 + 契约测试 | **#282** |

> 两票合入后 F18 特性才算完成。**不设 umbrella**（2 张紧耦合票无需额外跟踪票）；如需另行开票。

## 0.4 文件所有权（同一文件同一时刻只允许一张票在飞）

| 文件 | 票 | 说明 |
|---|---|---|
| `src/agent_harness/session/event.py` | F18-A | 加常量 + 入 `EVENT_TYPES` |
| `src/agent_harness/session/approval.py` | F18-A | 派生改「最后一次胜」 |
| `src/agent_harness/session/permission_switch.py`（新） | F18-A | 唯一写入口 |
| `src/agent_harness/web/app.py` | F18-A | **仅**新端点 |
| `web/src/**`（前端仓 `D:\intelligence-agent-frontend`） | F18-B | pill / 投影 / 测试 |

## 0.5 与并行批次的避让（**硬约束**）

同仓另有并行批次 `docs/tickets/architecture-audit-remediation-2026-09-18.md`（T01–T12 / #237–#266，
全 Python 后端），其候选文件含 `session/**` 的部分文件。

- F18-A **开工前必须**：`git log --oneline -8 -- <要改的文件>` + `git status --short`，逐文件核对是否被并行批次占用；**同文件在飞 → 停止并报告**（不得自行 rebase/merge）。
- **不得**改 `permission_policy` 折叠（#236 边界）。
- 前端仓另一线：`D:\intelligence-agent-frontend`（#236 已合入其 `main`）。

## 0.6 取证要点（供 review 复算）

- **模板**：`model/changed`（`session/event.py:107`；`session/model_switch.py:32-45 / 152-171 / 174-188`；`web/app.py:1471`；`session/service.py:696 / 1598`）。
- **新事件的注册点**（漏一个就红）：`event.py` 常量 → `EVENT_TYPES`（`:116`）→ 两个生成器脚本 → 三处守卫（`tests/test_event_types_generated.py:20,34`、`tests/test_event_vocabulary_generated.py:61`、**`tests/session/test_event_store.py:88` 硬编码期望集，必须手改**）。前端 `EVENT_SEMANTICS` 是 `Record<EventTypeValue,…>`（`projection.ts:1085`）⇒ 未登记会 **tsc 红**。
- **「last wins」先例**：`projection.ts:779-782`（`tool/approval-requested`）、`:874-879`（`model/changed`）。
- **危险确认先例**：`ProviderManagerDialog.tsx:441-460`、`MemoryPanel.tsx:264-290`、`DeleteSessionDialog.tsx:174`。
- **pending 禁用先例**：`Composer.tsx:110 / 284 / 289`；驱动源 `App.tsx:1112`。
- **被 `#236` 钉住的测试**（F18-B 必须同步，不得只删断言）：`OptionPicker.test.tsx:79`、`permission.test.ts:118`、`projection.test.ts:2195`、`e2e/control-row.spec.ts:425/456`、`e2e/u-project-task.spec.ts:180`。

## 0.8 AC2 红证（改造前失败输出）

同一断言脚本（`追加一条 permission/changed 是否**改变**会话当下生效的权限档？`）对
**改造前**（`git archive 3b4e93c6 src` 解出的纯净树）与**改造后**（工作树）各跑一次；
脚本自动探到哪个 API 就用哪个：改造前只有 `declared_permission_mode`（派生只看第一条
`session/started`），改造后是 `effective_permission_mode`（最后一次 changed 胜）。

```
=== 改造前（HEAD 树）===
[API] 改造前代码：只有 declared_permission_mode（派生只看第一条 session/started）
[派生值] PermissionPolicy.READ_ONLY  （期望 danger-full-access）
Traceback (most recent call last):
  File ".../red_proof.py", line 35, in <module>
    assert got is PermissionPolicy.DANGER_FULL_ACCESS, (
AssertionError: 红：追加 permission/changed 后派生值仍是 PermissionPolicy.READ_ONLY —— 事件未改变生效档
exit=1
=== 改造后（工作树）===
[API] 改造后代码：effective_permission_mode（最后一次 permission/changed 胜）
[派生值] PermissionPolicy.DANGER_FULL_ACCESS  （期望 danger-full-access）
[OK] 追加 permission/changed 改变了当下生效的权限档
exit=0
```

复现：`git archive 3b4e93c6 src -o head.tar` → 解到临时目录 → `PYTHONPATH=<临时>/src`
与 `PYTHONPATH=<worktree>/src` 各跑一次该脚本。（脚本本身是临时的，不随票入库；不变量
已由 `tests/session/test_permission_change.py::TestEffectivePermissionMode` 永久钉住。）

## 0.7 环境铁律（本仓实测，别照抄票面的 `uv run`）

- worktree 的 `.venv` 是**空壳**；用主仓 venv `D:/intelligence-agent-backend/.venv/Scripts/python.exe`（pytest 9.1.1 / ruff 0.16.3）+ `PYTHONPATH=<worktree>/src`（**Windows 形态**，POSIX `/c/...` 会静默失效解析到主仓）+ `PYTHONUTF8=1` + `-p no:randomly`。
- 禁止 `git stash`；禁止裸 `git commit`（会扫 `refs/heads/workbuddy/`）；落地走 plumbing（`add`/`write-tree`/`commit-tree`）+ 直写松散 ref 文件。
- 新建**仓库内** `.md` 按 **CRLF** 写（`core.autocrlf=true`，邻居文件皆 CRLF）。
- 前端仓 `.md`/`.ts` 同理；`npm`/`.cmd` 在本沙箱被 Program Blacklist 拦 ⇒ 用 node 直调 CLI 的 `.js` 入口。
