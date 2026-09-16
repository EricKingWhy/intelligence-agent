/** 目录条目 `icon` 语义名 → 行首图标（#201 冻结行「每行 20px 图标槽」的呈现数据）。
 *
 * ## 数据来源：后端声明的**语义名**，不是前端按 id 猜
 *
 * #214 之前这里是**按条目 id** 映射（`read-only` → 锁、`main` → 分层…）。那样只有内置
 * id 有图标：目录一旦被扩展（新档位、新权限模式、部署自带条目），那一行永远没有图标——
 * 前端没有渠道知道该画什么。现在三个目录端点（`/api/permission-modes`、
 * `/api/reasoning-efforts`、`/api/agent-profiles`）在内置条目上下发 `icon` 名
 * （后端 `web/app.py::CATALOG_ICON_NAMES` 是取值集的单一归属），本模块把**已知名**
 * 映射成字形。
 *
 * ## 未知名 / 缺键：留空槽，不编字形
 *
 * 名不认识、条目压根没带 `icon`（老载荷、部署自定义档位）→ 一律 `undefined` ⇒ 那一行的
 * 图标槽**渲染成空的 20px**（对齐不破，见 `OptionPicker`：槽恒在、内容可空）。给未知名
 * 编一个字形、或回头拿 id 去猜，都是本产品明令禁止的「编占位」（PRODUCT.md 原则 3
 * 「真实优先于好看」）——id 空间是可扩展的，猜出来的字形只是在宣称一个后端没说的语义。
 *
 * 纯函数、无 JSX（返回组件类型，尺寸由渲染层决定）——vitest 直测。 */

import { Brain, Code2, FileSearch, Layers, Lock, Pencil, Telescope, Unlock, Zap, type LucideIcon } from 'lucide-react';

/** 已知图标名 → 字形。名是**语义**（锁 / 笔 / 开锁），不是字形描述——换字形不改名，
 *  这样后端声明的名不会因为前端换了个更顺眼的图标而失效。 */
const ICONS = {
  // 权限模式：能不能写、要不要审批
  lock: Lock,
  pencil: Pencil,
  unlock: Unlock,
  // agent 档位：全工具 / 写代码 / 查与读
  layers: Layers,
  code: Code2,
  search: FileSearch,
  // 推理深度：最快 / 平衡 / 最深
  bolt: Zap,
  gauge: Brain,
  telescope: Telescope,
} satisfies Record<string, LucideIcon>;

/** 已知名全集。与后端 `web/app.py::CATALOG_ICON_NAMES` **同集**——跨语言、跨目录的
 *  手工镜像（同 `lib/capabilities.ts::SURFACE_KEYS` 的既有口径），两端各有测试锁，
 *  增名必须一起改。 */
export const CATALOG_ICON_NAMES: ReadonlySet<string> = new Set(Object.keys(ICONS));

/** 条目的行首图标；未知名 / 缺键 ⇒ `undefined`（渲染层留空槽，绝不编字形）。 */
export function catalogIcon(iconName?: string | null): LucideIcon | undefined {
  return iconName && CATALOG_ICON_NAMES.has(iconName)
    ? ICONS[iconName as keyof typeof ICONS]
    : undefined;
}
