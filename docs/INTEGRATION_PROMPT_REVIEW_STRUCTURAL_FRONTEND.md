# 集成提示词 — 全分支结构轴复审（前端半）

> 面向 **Git Integrator**。本文件说明 `feat/frontend` 上新增的复审修复，供合入 `main` 前核查。
> 日期：2026-09-15 ｜ 分支：`feat/frontend` ｜ **未 push、未 merge**（留给集成方）

## 1. 本批 commit

| commit | 说明 |
| --- | --- |
| `f766848` | `refactor(review): 结构复审修复——供应商多模型编辑 + 队列条动作带 queue_id` |

后端半为独立 worktree 的 `d2aa803`，见 `INTEGRATION_PROMPT_REVIEW_STRUCTURAL_BACKEND.md`（后端 worktree 内）。
**两半需一起合**；两者无相互依赖的契约变更，顺序无关。

## 2. 复审轴与范围

复审轴 = **代码整洁度**（重复、死代码、过载函数、抽象泄漏、注释噪音、命名漂移、真 bug），
刻意不重复"票面是否满足"（前一轮终审已覆盖）。本批**不新增票**、不关单。

## 3. 真 bug（用户可见，已修）

| # | 问题 | 修法 |
| --- | --- | --- |
| ① | 供应商表单把 `models` **整表替换**成只含 `models[0]` 的一行，保存即**静默删掉其余模型**（override 内置条目因后端做并集尤其明显）；`label` 同样被丢，等于删掉用户已有显示名 | 改为多行编辑（每行 `model_id` + 可选 `label`），符合 ADR-0032 §8.1「模型列表（可增删行，每行 model_id + 可选 label）」 |
| ② | create 分支 `api_key` 被两个 spread **重复写入**，后者恒覆盖前者 | 收敛为单个 `...(apiKey ? { api_key: apiKey } : {})` |
| ③ | 队列条「立即」未带 `queue_id` → 原排队项仍在队列，终态驱动会**把同一句再投递一次**（同内容处理两遍） | 改为发 `amend: { mode: 'steer', queue_id: item.id }`（ADR-0030 §5.2：立即 = 升级为 steer + 先取消原排队项） |
| ④ | 队列条为 **steer 项**也渲染 编辑/立即/取消，但后端定位只认 `queue_id`（`cancel_queue` 只认 `kind=queue`）→ 点击即**静默 404** | 三个操作按钮仅对 `item.kind === 'queue'` 渲染 |
| ⑤ | 「编辑」原为"回填主输入框 + 取消原项"，与 §5.2 的**就地编辑**不符；且取消失败会留下"已预填却仍排队"的双重状态 | 改为行内编辑态（`editingId`/`editDraft`），提交 `{content, queue_id}`，不回填主输入框 |

## 4. 结构性去重

- 模型列表规整抽成**纯函数** `web/src/lib/providerModels.ts`（前端 vitest 仅 SSR、无 @testing-library/jsdom，纯函数才可直测），不再埋在组件里。
  新增 `web/src/lib/providerModels.test.ts`，6 例：保留全部行 / 保留 label / trim 并省略空 label / 丢弃空 model_id / 首个优先去重 / 全空 → `[]`。
- CSS 新增类均用 token，无 craft-floor 违规（无渐变、无 >1px 彩色 border-left）。

## 5. 新增测试

- `src/lib/providerModels.test.ts`：6 例（见上）。
- `src/components/Composer.test.tsx`：新增 `Composer 队列条（ADR-0030 §5.2）` 块 3 例——空队列不渲染、queue 项渲染三个中文 aria 操作、**steer 项不渲染这三个操作**。
- `e2e/multiturn-queue.spec.ts`：T12c（「立即发送」请求体带 `queue_id`）、T12d（就地编辑**不回填主输入框**）。

## 6. 门禁证据

```
tsc -b                                → clean
vitest run                            → 848 passed（829 → 848，+9）
oxlint                                → 0 error / 43 warnings（均既有，无一来自本批改动文件）
playwright test --workers=2           → 346 passed（含新增 T12c/T12d）
vite build                            → 绿
```

## 7. 记为债务、未修（Scope Lock）

- `src/hooks/useSession.ts` 流前置代码 4 处（≈715/786/845/1123）——**实质不同**，非机械重复，故不重构（轻率合并会改变重放/取消语义）。
- `getContextProviders` 导出未被前端引用（端点仍在，保留决定）——**保留，不删**，属既有的显式决定。
- CSS 主题变量：本批**未新增 token**，§15 双块同步规则不涉及。

## 8. 集成方待办

```bash
git -C D:\intelligence-agent fetch origin
git diff main...feat/frontend       # 核查
# 在 main worktree：merge feat/frontend（需用户明确批准）
```

**未做且不应擅自做**：`git push`、merge 到 `main`、创建 PR、删除分支/worktree（AGENTS.md §13.2 / §14.4）。
