# 集成提示词 — 前端 B-3 批次（#194 / #197 / #199 / #201，Web UI 重设计）

> **给集成 AI（在 `D:\intelligence-agent` 的 `main` 上操作）**。本文件在 `feat/frontend`
> worktree 内；§0 是可执行摘要，§1–§5 是合并前必须知道的契约与残余。
> 来源设计稿：`docs/design/WEB_UI_BATCH_REDESIGN.md`（已随本批镜像进 `feat/frontend`）。
> 进度单一事实源：`docs/SDD_TICKET_TRACKER.md` 的 **B-3 台账 + B-3 交付记录**。

---

## §0 可执行摘要

| 项 | 值 |
| --- | --- |
| 分支 | `feat/frontend`（`D:\intelligence-agent-frontend`） |
| 交付 commit | `5cfb6ff`(#197) → `0221080`(#194) → `4ddec6b`(#199+#201) → `cd3a2b4`(B-3 两轴 review 修复) → `5b5f940`/tracker |
| 本批 fixed point | `c00604e`（文档镜像 commit，本批第一行代码之前） |
| 审查状态 | B-3 两轴（Spec + Standards）**已审并全部处置**；下一批 fixed point = `cd3a2b4` |
| 合并顺序 | 单分支合并：`feat/frontend` → `main`（本批无后端改动，不涉及跨端顺序问题） |
| 关闭状态 | #194/#197 **已关单**（代码完成、未合入 main，按 §14.12）；#199/#201 **保持 OPEN**（各有 1 条冻结 AC 因缺后端数据未落地，见 §3） |
| 本批是否需要后端配合 | 不需要即可合并运行；#199/#201 的残余 AC 依赖 #203（供应商管理）补字段 |

**合并前最小验证（在 `D:\intelligence-agent\web`）**：

```bash
npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build
```

预期：`tsc` 0 error · vitest **828 passed** · oxlint **0 error**（43 warnings 为既有基线）·
playwright **326 passed**（两个 viewport：chromium-1280 / chromium-1920）· `vite build` 成功。

---

## §1 这批改了什么（按票）

### #197 Inspector 拖拽方向 + 默认宽度（`5cfb6ff`）
- 指针：`width = clampInspectorWidth(drag.startW − (e.clientX − drag.startX), available)`。
  **语义**：Inspector 是**最右列**、把手在它的**左缘** ⇒ 向左拖 = 变宽（原实现是反的）。
- 键盘同源：`width + (ArrowRight ? −step : +step)`。
- `INSPECTOR_DEFAULT_W = 340`（回访 / 首次渲染用）；**必须 > `INSPECTOR_MIN_W`(320)**，
  否则右拖落在 min 上表现为「拖不动」。
- 只动了 2 个符号 + 1 个常量；`clampInspectorWidth`、把手 DOM、`role="separator"`、
  面板内部业务代码按票面约束**未动**。
- 同步断言：`y-inspector-peek.spec.ts`（指针/键盘双向 + 刷新后仍是 340）、
  `workspace-modes.spec.ts` 第三轨 320→340（设计稿的测试影响清单**漏了这一处**，是本票补的）。

### #194 Esc 提示不再压档位控件（`0221080`）
- `.composer-esc-hint` 从「绝对定位左下角」移入新的 `.composer-actions` 动作簇
  （与发送/停止按钮同簇，`right/bottom: var(--space-sm)`）。
- 只为流式期间预留空间：`.composer-dock:has(.composer-esc-hint) .composer-controls { padding-right: var(--composer-actions-w) }`——
  **变量定义在 `.composer-dock` 上**（曾误定义在 `.composer-actions` 上导致 `var()` 落空，
  注释里有记录，别再改回去）。
- 停止按钮视觉位置像素级未变（`.composer-stop` 由绝对定位改为簇内 `position: static`）。

### #199 模型选择器两级飞出（`4ddec6b`）
- `ModelPicker` 从「Popover + cmdk 扁平可搜索列表」改为 **Radix `DropdownMenu` 两级飞出**：
  一级 = `默认链` + provider 行（`menuitem` + `aria-haspopup="menu"`，行尾「N 个模型」）；
  二级 = 该 provider 的模型（`menuitemradio` + `aria-checked`，走 `RadioGroup`）。
- **删搜索框**（用户裁定）；保留 `groupByProvider()`、`默认链` 伪选项、`commitSelection`
  的 BUG-011 去重守卫（回归锁 `e2e/q-model-dedupe.spec.ts` 仍在，且验证过去掉守卫会转红）。
- ⚠ **语义变更（刻意，已记）**：`listbox`/`combobox` 不再存在，打开信号变成 `[role="menu"]`。
  凡是有测试或脚本依赖模型选择器的 a11y 角色，必须按菜单/子菜单语义改写。

### #201 三个档位下拉统一为 `OptionPicker`（`4ddec6b`）
- 新增共享组件 `web/src/components/OptionPicker.tsx`；`ControlPicker` 与多选
  `ContextProviderPicker`（含各自单测与 `e2e/context-providers.spec.ts`）**删除**；
  4 个调用点（Composer 权限/档位/深度 + `StartTaskInProjectDialog`）迁移。
- 选中态三通道：勾选图标 + 标题加重 + 左侧 2px accent 条，经 **`data-state="checked"`** 表达
  ——**不要**改用 `data-selected`（cmdk 的 `CommandItem` 会用自己的高亮值覆盖该属性）。
- CSS：原 `.model-picker-*` 这层壳正名为 `.picker-*`（它本就同时服务多个控件），
  死规则 `.model-picker-group-label` 删除，三处重复的触发按钮样式合并为
  `.composer-trigger-*`。

---

## §2 合并时必须守住的契约（本次改动**没有**碰这些）

1. **`context_providers` 请求契约一字未动**：`POST /api/sessions` 与 `/messages` 仍接受该字段
   （空数组 = 不发键），只是 UI 不再有选择入口。`web/src/lib/api.ts` / `api.test.ts` **未改**；
   `amend.ts` 仅下线 UI 侧映射。程序化调用路径完整可用。
2. **`aria-label` 维持原中英混用**：`权限模式`（中文）/ `Agent Profile` / `Reasoning Effort`（英文）。
   票面冻结结论 B 明确「会影响到 e2e 定位器就不统一中文」。实现里第一版曾顺手统一成中文，
   被两轴 review 的 Spec 轴判为 **P1** 并已回退——**不要再统一**（`Composer.tsx` 有注释写明）。
3. **`OptionPicker` 不内置 `aria-label` 默认值**：由调用方传（三处默认值打架正是原 e2e 定位器
   误伤的来源）。
4. **`INSPECTOR_DEFAULT_W`(340) > `INSPECTOR_MIN_W`(320)** 是硬约束，不是巧合。
5. 主题 token：本批未新增颜色/尺寸 token；若后续要加，按 AGENTS §15 在两个主题块都定义。

---

## §3 本批的残余（**不关单的两票就是为此**，请勿在集成时"顺手补"）

| # | 残余 | 为什么没做 | 要做需要什么 |
| --- | --- | --- | --- |
| 1 | #199「不可用 provider 置灰但仍可展开 + 行尾原因」 | `/api/models` **有** `is_available` 但后端无条件写死 `True`（`web/app.py::_render_model_option`：「catalog 无 disabled 概念」），且 `unavailable_reason` 字段不存在 ⇒ 没有任何 provider 会是不可用态，置灰分支永远不触发 | 后端在模型目录里真实表达不可用 + reason（#203 范围） |
| 2 | #199 一级底部「管理模型」入口 | 它是 **#203 的交付物**；现在放上去只能是死入口 | #203 的供应商管理弹层落地后接上（`.picker-foot` 槽位已预留） |
| 3 | #201「档位收窄提示：该档位只开放 N 个工具（共 M 个）」 | `GET /api/agent-profiles` 只回 `display_name`/`description`，**不暴露工具数** ⇒ N/M 无来源（编数字违反「不编占位」） | 后端在 agent-profiles 响应里暴露工具数；前端 `OptionPicker` 的 `footer` 插槽已按设计稿预留 |
| 4 | #201 每行 20px 图标槽 | `CatalogEntry = {id, display_name, description}` **无 per-option 图标数据**；为后端可扩展枚举编字形就是编占位 | 目录契约补图标字段 |
| 5 | #199 二级行次级文案 = 真实 model id / `默认`（设计稿写的是 provider 名） | provider 名在「某个 provider 的展开」里是冗余信息，会把描述行浪费掉 | 若产品坚持要 provider 名，改 `ModelPicker.modelMeta()` 并在两主题下复检对比度 |

---

## §4 集成时的注意点（都是本批踩过的）

1. **Playwright 必须 `--workers=2`**（§16.6）：4 worker 全量并行有资源竞争型抖动。
2. **5173 端口的 dev server 是哪个 worktree 的**要确认：`reuseExistingServer` 在本地为真，
   跑 e2e 前确认 5173 服务的是 `D:\intelligence-agent` 这份代码，否则测的是别的分支。
3. **Radix 浮层的两个真实时序坑**（探针实测，已写进 `e2e/fixtures.ts` 注释）：
   - 移焦发生在 `setTimeout` 里 ⇒ 按键后**立刻**读 `document.activeElement` 会读到旧值
     （用 `pressMenuItemKey` 的轮询，别写成瞬时断言）；
   - 退出动画窗口内再开浮层会被正在卸载的 modal 菜单吞掉（`body{pointer-events:none}` +
     焦点被抓回残留节点）⇒ helper 首尾各等一次「菜单已卸载」。
4. **不要为了让某个 e2e 变绿而放宽断言**：本批的「无搜索框」断言曾因数错类名而**恒真**，
   被 review 挑出后改成按 `input`/`role=combobox`/`[cmdk-input]` 数并加了反向对照。
   同类假绿请按「变异验证」（把功能改坏 → 该测试必须红）自查。
5. 本批**未推远程**（§13.2/§14.4）：`feat/frontend` 的 push、`main` 的合并与 push 都由集成侧执行。

---

## §5 复核命令

```bash
# 差异全貌（与 main 比）
git -C D:/intelligence-agent diff main...feat/frontend --stat

# 本批两轴审查范围（fixed point → 修复）
git -C D:/intelligence-agent-frontend diff c00604e..cd3a2b4 --stat

# 只跑本批受影响的 e2e（比全量快得多）
cd D:/intelligence-agent/web
npx playwright test e2e/model-picker.spec.ts e2e/q-model-dedupe.spec.ts e2e/control-row.spec.ts \
  e2e/picker-search-visibility.spec.ts e2e/continuation.spec.ts \
  e2e/composer-stream-actions.spec.ts e2e/y-inspector-peek.spec.ts e2e/workspace-modes.spec.ts --workers=2
```
