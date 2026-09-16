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
 * ## 名单是跨语言契约：两侧各写一份，由测试对账（#217）
 *
 * `CATALOG_ICON_NAMES`（下面那份字面量）与后端 `web/app.py::CATALOG_ICON_NAMES` 必须**同集**，
 * 两道机械闸门各自负责一半：
 *   ① **编译期**——`ICONS` 的类型是 `Record<CatalogIconName, LucideIcon>`，所以"声明了名却
 *      忘了配字形"（或配了名单外的名）过不了 `tsc`；
 *   ② **门禁期**——后端 `tests/web/…::TestCatalogIcons::test_frontend_mirror_is_in_sync_with_backend_set`
 *      的跨端对账**直接读下面那份字面量**，与后端集合逐值比对，所以"单边增删名"会红。
 * ② 是 2026-09-17 补的（#217）。此前两侧只有各自钉住自己那份的测试：**后端侧**其实已被
 * `TestCatalogIcons` 的"集合 == 实际下发的并集"与逐 id 映射挡住，**前端侧**才是真的开着——
 * 前端那把锁比的是前端自己的镜像字面量，于是"前端删掉一个后端仍在下发的字形"两套测试全绿，
 * 界面上那一行只是**静默变空槽**（正是 #214 要消灭的那类静默，而且用户可见）。
 *
 * 为什么不做代码生成（后端集合 → 前端字面量）：仓里确有这个范式（`scripts/gen_event_types.py`
 * → `web/src/generated/event-types.ts`，由 `tests/test_event_types_generated.py` 兜底），但它
 * 生成的是**整份**模块；这里前端仍需为每个名**手写字形**（生成不出字形的选择），所以生成只
 * 能省掉那 9 行名单，却要多养一对"生成脚本 + 生成物"和它的兜底测试——代价大于收益。等值对账
 * 落在既有的两道门禁里，零新增基础设施。
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

/** 已知名全集——**前端唯一的名单声明**。名是**语义**（锁 / 笔 / 开锁），不是字形描述：
 *  换一个更顺眼的字形不改名，后端声明的名因此不会失效。
 *
 *  ⚠ 增删名要**同一个提交里改两端**（此处 + 后端 `web/app.py::CATALOG_ICON_NAMES`）：
 *  漏改前端 ⇒ 这一行空槽且两侧测试会红（后端对账读的就是这份字面量）；漏改后端 ⇒ 没有
 *  条目会下发它（后端 `TestCatalogIcons` 的"集合 == 实际下发的并集"那条会红）。 */
export const CATALOG_ICON_NAMES = [
  // 权限模式：能不能写、要不要审批
  'lock',
  'pencil',
  'unlock',
  // agent 档位：全工具 / 写代码 / 查与读
  'layers',
  'code',
  'search',
  // 推理深度：最快 / 平衡 / 最深
  'bolt',
  'gauge',
  'telescope',
] as const;

/** 名单里的名（上面那份字面量的编译期联合类型）——只服务本文件的 `ICONS` 定型与取字形时的
 *  类型断言，故不导出：等真的有外部消费方（例如某个 prop 要收这个名字）再开出去。 */
type CatalogIconName = (typeof CATALOG_ICON_NAMES)[number];

/** 已知名 → 字形。类型是 `Record<CatalogIconName, LucideIcon>` ⇒ "名单里有名、这里没字形"
 *  与"这里多一个名单外的名"都在**编译期**被拒（`tsc` 是这条不变量的执行者，不是测试）。 */
const ICONS: Record<CatalogIconName, LucideIcon> = {
  lock: Lock,
  pencil: Pencil,
  unlock: Unlock,
  layers: Layers,
  code: Code2,
  search: FileSearch,
  bolt: Zap,
  gauge: Brain,
  telescope: Telescope,
};

const KNOWN: ReadonlySet<string> = new Set(CATALOG_ICON_NAMES);

/** 条目的行首图标；未知名 / 缺键 ⇒ `undefined`（渲染层留空槽，绝不编字形）。 */
export function catalogIcon(iconName?: string | null): LucideIcon | undefined {
  // 入参是任意字符串（载荷来自后端）：先过集合闸门，再**断言**成联合类型取字形——
  // 这是这个 cast 成立的唯一理由（`KNOWN.has()` 对 `ReadonlySet<string>` 不做收窄）。
  return iconName && KNOWN.has(iconName) ? ICONS[iconName as CatalogIconName] : undefined;
}
