/** 档位收窄提示的文案组装（#201 冻结 AC 的唯一实现点）。
 *
 * 用户裁定（设计稿 §4）：「要提示，但从简，不能突兀」——位置固定为档位 picker
 * 弹层的 `footer`，一行次级文字，被收窄掉的名字放 `title`，**不用** toast /
 * banner / 一次性弹窗。
 *
 * 为什么单独成模块：这是**纯函数**（`CatalogEntry[]` + 当前档位 → 一句话），
 * vitest 能直测（本仓 vitest 是 SSR、弹层内容断言不了，交互只能靠 playwright；
 * 但"该说什么话"这件事不该既依赖后端又依赖浏览器）。弹层里只做渲染。
 *
 * 口径（三条都与后端对齐，抄在这里方便复核）：
 *   - 数字来自 `GET /api/agent-profiles` 的 `tool_scope`（后端 `tool_scope_summary`）；
 *   - 后端**没给** `tool_scope`（老部署 / 夹具没带）⇒ 返回 null，不显示——宁可不提示，
 *     也不编一个数；
 *   - `excluded` 为空（该档位没被收窄，例如「通用」）⇒ 也返回 null：
 *     「共 17 个中开放 17 个」只是噪音，用户没被收窄就没有事实要披露。
 */

import type { CatalogEntry } from './api';

/** tooltip 里最多列几个工具名（设计稿 §4：「最多 6 个 + 「…」」）。 */
export const MAX_LISTED_TOOLS = 6;

/** 未选档位（`DEFAULT_VALUE` / null）时的后端默认档位。
 *
 * `agent_profile=None` 在运行时落到 `main`（`assembly.py:401`
 * `agent_profile if agent_profile is not None else "main"`），所以"默认"要按 main 的
 * 工具面披露；编成别的档位等于替用户报错一个数。 */
export const DEFAULT_PROFILE_ID = 'main';

export interface ToolScopeNote {
  /** footer 那一行文字（逐字对应设计稿 §4）。 */
  text: string;
  /** hover/聚焦时的 `title`：被收窄掉的工具名（最多 6 个 + 「…」）。 */
  title: string;
}

/** 当前档位的收窄提示；无可披露的事实 → null。
 *
 * @param entries `GET /api/agent-profiles` 的条目
 * @param effectiveProfileId 当前生效档位 id；null / '' = 未选（按后端默认档位算）
 */
export function toolScopeNote(
  entries: CatalogEntry[],
  effectiveProfileId: string | null | undefined,
): ToolScopeNote | null {
  const id = effectiveProfileId && effectiveProfileId.length > 0
    ? effectiveProfileId
    : DEFAULT_PROFILE_ID;
  const entry = entries.find((e) => e.id === id);
  const scope = entry?.tool_scope;
  if (!scope) return null;
  if (scope.excluded.length === 0) return null;
  const listed = scope.excluded.slice(0, MAX_LISTED_TOOLS);
  const more = scope.excluded.length > listed.length;
  return {
    text: `该档位只开放 ${scope.open} 个工具（共 ${scope.total} 个）`,
    // 只列名字、不加解释——设计稿明说"只说事实，不解释原因"（原因属于产品文档，
    // 而这个 tooltip 的位置只够一行）。
    title: `未开放：${listed.join('、')}${more ? '…' : ''}`,
  };
}
