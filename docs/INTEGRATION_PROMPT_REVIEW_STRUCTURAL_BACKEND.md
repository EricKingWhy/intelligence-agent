# 集成提示词 — 全分支结构轴复审（后端半）

> 面向 **Git Integrator**。本文件说明 `feat/backend` 上新增的复审修复，供合入 `main` 前核查。
> 日期：2026-09-15 ｜ 分支：`feat/backend` ｜ **未 push、未 merge**（留给集成方）

## 1. 本批 commit

| commit | 说明 |
| --- | --- |
| `d2aa803` | `refactor(review): 结构复审修复——used_tokens 漏报 + 去重 + 单一接缝` |
| `19809b5` | `docs(phase-status): record full-branch structural review + fixes` |

前端半为独立 worktree 的 `f766848`，见 `docs/INTEGRATION_PROMPT_REVIEW_STRUCTURAL_FRONTEND.md`（前端 worktree 内）。**两半需一起合，但顺序无关**（无相互依赖的契约变更）。

## 2. 复审轴与范围

本次复审轴是 **代码整洁度**：重复、死代码、过载函数、抽象泄漏、注释噪音、命名漂移、真 bug。
刻意**不**重复"票面/规格是否被满足"——那一轴前一轮终审已覆盖。因此本批**不新增票**、不关单。

## 3. 最高严重级真 bug（用户可见，已修）

`ContextBuilder.usage_snapshot` 的 `used_tokens` 此前等于 `_token_estimate_total`，而后者**只含投影 messages**。
system_prompt 与全部 context provider 注入都不在其中。后果：

- 看板**静默漏报**总量（不是差一点，是只报一部分）；
- `other`（记忆注入）桶**恒为 0**——provider 注入从未被记账。

**可执行探针**（受控注入场景）：旧口径 `used_tokens = 66`，真实构建总量 `= 544`；修复后 `= 544` 且 `other = 352`。

修法：按 provider 名称归因（`_last_provider_tokens_by_name` + `_last_runtime_context_tokens`），
`used_tokens = Σ各桶`，`usage_snapshot` 去掉 `skills_tokens` 入参（每个桶都有真实来源，不再靠外部补数）。

同时**删除** `web/context_usage.py::skills_provider_tokens()`：它复制 `select()` 的文本、访问 `_capability` / `_DATA_FRAME`
私有面、且**跳过预算截断**，与真实注入成本必然漂移。注：前一轮"让 live 与收口共享同一个函数以保持一致"的方向本身是错的——
一致地错不如各自真实。`build_context_usage_payload` 里无效的 `other += max(...)`（恒等加 0）一并删除。

## 4. 结构性去重（同一语义多份定义 → 单一接缝）

- **`ProviderStore` 4 处构造 → 唯一 `for_settings()`**：原实现有 3 种不同的 `builtin_ids` 取值，其中 2 处漏传 `PROVIDER_PRESETS`，
  会让 override/custom 的 `kind` 派生出错。现在唯一构造边界是 `ProviderStore.for_settings(settings)`。
- **`_now_iso` / `_now` 重复 → 公开 `utc_now_iso()`**（`_now` 由私有转公开，2 处调用点更新）。
- **`app.py` 3 份 SSE 生成器 → 模块级 `_run_stream_response()`**（create / resume / launched 三处收敛；`STREAM_REPLAY_MAX_EVENTS = 1000` 及注释原样保留）。
- **资源释放**：`run.runtime = None`——`_runs` 每会话终身保留一条 `ManagedRun`，不置空则泄漏整条 runtime 引用链。
- **文档**：`ProviderStore` 模块 docstring 的导入方向错述已改正；`docs/design/CONTEXT_CAPACITY_DASHBOARD.md` §3.2 桶表与"实现口径"段按新口径重写（明确禁止残差扣除）。

## 5. 新增回归测试

- `tests/web/test_context_usage.py::test_t4_snapshot_counts_provider_injection`
- `tests/web/test_context_usage.py::test_t4_snapshot_attributes_skills_separately`

两条锁住：**桶 = 真实注入成本**、**总量 = Σ桶**。旧口径下这两条必红。

## 6. 门禁证据

```
ruff check            → clean
pytest（全量）         → 2382 passed / 10 skipped / 42 deselected
```

## 7. 已知 flake（既有，非本批引入）

`tests/web/test_multiturn_queue_http.py::test_get_queue_and_flush_roundtrip` 约 1/5 概率失败，两种模式
（flush 未返回 SSE 流 / `409` vs `200`）。因本批改动了 flush 的 SSE 路径，特在**修复前 commit `552a5e5`** 建临时 worktree 复现
（8 跑 1 败）后删除，**证明是既有 flake**。合入前若遇到，按 flake 处理（重跑），不要当成回归。

## 8. 记为债务、未修（Scope Lock）

- `web/src/hooks/useSession.ts` 流前置代码 4 处（≈715/786/845/1123）——**实质不同**，非机械重复，故不重构。
- `getContextProviders` 导出未被前端引用（端点仍在，保留决定）。

## 9. 集成方待办

```bash
git -C D:\intelligence-agent fetch origin
git diff main...feat/backend        # 核查
# 在 main worktree：merge feat/backend（需用户明确批准）
```

**未做且不应擅自做**：`git push`、merge 到 `main`、创建 PR、删除分支/worktree（AGENTS.md §13.2 / §14.4）。
