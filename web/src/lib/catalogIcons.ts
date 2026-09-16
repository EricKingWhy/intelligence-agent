/** 目录条目 id → 行首图标（#201 冻结行「每行 20px 图标槽」的呈现数据）。
 *
 * ## 为什么是**按已知 id 的映射**，而不是从后端来的字段
 *
 * 三份目录的条目契约是 `CatalogEntry {id, display_name, description}`，**没有** per-option
 * 图标数据。这里给的是**内置 id → 字形**的映射，它不是"编占位"，而是与"选中态用哪种
 * 颜色"同一类**呈现选择**：`read-only` 是只读、`workspace-write` 是写入、`main` 是通用
 * 编排——每个字形都对应这个档位**真实存在的语义**，不宣称任何数据。
 *
 * ## 未知 id：留空槽，不编字形
 *
 * 目录是可扩展的（后端加一个档位、夹具里就有 `mode-0…mode-5`），未知 id 一律返回
 * `undefined` ⇒ 那一行的图标槽**渲染成空的 20px**（对齐不破，见 `OptionPicker`：
 * 槽恒在，内容可空）。给未知 id 编一个字形才是本产品明令禁止的「编占位」
 * （PRODUCT.md 原则 3「真实优先于好看」）。
 *
 * 这条残留缺口（部署自定义的档位/模式拿不到图标）已开 issue 登记：
 * 需要后端在目录条目上给一个可选 `icon` 键，前端再把已知 icon 名映射成字形。
 *
 * 纯函数、无 JSX（返回组件类型，尺寸由渲染层决定）——vitest 直测。 */

import { Brain, Code2, FileSearch, Layers, Lock, Pencil, Telescope, Unlock, Zap, type LucideIcon } from 'lucide-react';

/** 内置档位/模式 id → 图标。
 *
 * 三份目录的 id 空间**互不重叠**，所以一张表就够（不必按目录分三份，那只会多两个
 * 参数与三处同步点）：
 *   - 权限模式（`PermissionPolicy`，`tooling/contract.py:102-104`）
 *   - Agent 档位（`agent/profiles.py::BUILTIN_PROFILES`）
 *   - 推理深度（`web/app.py::REASONING_EFFORT_DESCRIPTIONS`） */
const ICONS: Record<string, LucideIcon> = {
  // 权限模式：锁 / 笔 / 开锁——与"能不能写、要不要审批"这个真实差异对应
  'read-only': Lock,
  'workspace-write': Pencil,
  'danger-full-access': Unlock,
  // Agent 档位：全工具 / 写代码 / 查与读
  main: Layers,
  coding: Code2,
  research_review: FileSearch,
  // 推理深度：最快 / 平衡 / 最深
  minimal: Zap,
  standard: Brain,
  deep: Telescope,
};

/** 条目的行首图标；未知 id ⇒ `undefined`（渲染层留空槽，绝不编字形）。 */
export function catalogIcon(id: string): LucideIcon | undefined {
  return ICONS[id];
}
