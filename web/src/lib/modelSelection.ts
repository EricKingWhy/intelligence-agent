/** 模型选择器的值 → 「要 POST 给 `/model` 的模型名」（第十一轮 FE-R11-02）。
 *
 * ## 为什么需要这层解析
 *
 * 后端清掉「会话级模型覆盖」的**合法入参**是 `GET /api/models` 里 `is_default` 那个条目的
 * **名字**：`session/model_switch.py::is_default_selection()` 命中即解析成
 * `to_model_id=None` 并写一条 `model/changed{to=None}`（= 回到默认链）。
 * `web/src/lib/api.ts` 的注释也写明同一件事："默认条目（is_default=true）也是合法 POST target"。
 *
 * 而选择器的「默认链」项提交的是 `null`。此前 `handleModelChange` 的 `if (selectedId && name)`
 * 会直接跳过 POST —— 于是**已有会话**上选「默认链」什么都不会发生：
 * 界面显示「默认链」，会话却继续用上一个非默认模型。
 *
 * 真机实证（会话 `fb3619c6`）：选 `glm-5.3-flash` → 新增 `model/changed{to=glm-5.3-flash}`；
 * 再选「默认链」→ 事件流**不新增任何事件**，Inspector 的 MODEL 区仍显示 `glm-5.3-flash`；
 * 同一屏内 composer 说「默认链」、Inspector 说 `glm-5.3-flash`。刷新也不会纠正
 * （picker 只反映本地 state，不从会话事件反推）。
 *
 * ## 契约
 *
 * - `selected` 非空 → 原样返回（用户显式选了具体模型）。
 * - `selected === null` → 返回 catalog 里 `default` 条目的名字（= 清覆盖）。
 * - 找不到 `default` 条目 → `null`：调用方**保持既有行为**（不发请求），不伪造目标。
 *   后端保证 `GET /api/models` 至少有一个默认条目，所以这条是防御性分支。
 */
import type { ModelCatalogEntry } from './api';

export function modelChangeTarget(
  selected: string | null,
  models: readonly ModelCatalogEntry[],
): string | null {
  if (selected) return selected;
  return models.find((m) => m.default)?.name ?? null;
}

/** 选择器的值 → Composer 的 `model` 状态：默认条目名归一为 `null`（#1016）。
 *
 *  `modelChangeTarget` 做的是**反向**（`null` → 默认条目名，供 `POST /model` 清覆盖）；
 *  本函数做**正向**（默认条目名 → `null`）。缺了这一层，选中二级那条「默认」会把
 *  `selectedModel` 固化成默认模型名（`App.handleModelChange` 的 `if (name !== null)`
 *  判据漏了"name 其实是默认条目"这一层），此后每次续聊都带着这个名字去后端 → 422。
 *
 *  第一性原理：「默认 / 继承默认链」是取值空间之外的状态，不是一个 id。一级
 *  「默认链」已是它的唯一入口（提交 `null`），二级不再渲染默认条目（见
 *  `selectableModels`）；本函数是"万一拿到默认条目名也归零"的兜底。 */
export function normalizeSelectedModel(
  selected: string | null,
  models: readonly ModelCatalogEntry[],
): string | null {
  if (selected === null) return null;
  return models.find((m) => m.name === selected)?.default ? null : selected;
}

/** 二级列表**可选项**：排除默认条目（#1016）。
 *
 *  默认条目不是一个真实模型，而是"清除会话级覆盖"的状态；把它留在二级列表里，
 *  用户点它就会把 `selectedModel` 固化成默认模型名（见 `normalizeSelectedModel`），
 *  且与一级「默认链」入口语义重复。一级「默认链」是默认的唯一入口。
 *  过滤掉默认条目后，只剩默认条目的 provider 组会因 `groupByProvider` 无项而不渲染。 */
export function selectableModels(
  models: readonly ModelCatalogEntry[],
): ModelCatalogEntry[] {
  return models.filter((m) => !m.default);
}

/** **当前生效的目录条目**：显式选中项；未选（默认链）则目录里 `default` 那条；
 *  目录为空 / 名字不在目录里 → undefined（不认识就说不认识，不猜一条）。
 *
 *  #825（MM-04）AC7 用它取 `supportsVision`：附图入口的可用性、以及历史轮次的
 *  「图已被省略」标注，都必须与**这次真正会用的模型**一致——`App`（消息区标注）
 *  与 `Composer`（入口门禁）共用这一处解析，避免两边各写一遍"谁是当前模型"
 *  而漂移（漂移的后果是一边禁用、另一边却在图上标"已省略"）。 */
export function effectiveModelEntry(
  selected: string | null,
  models: readonly ModelCatalogEntry[],
): ModelCatalogEntry | undefined {
  if (selected) return models.find((m) => m.name === selected);
  return models.find((m) => m.default);
}
