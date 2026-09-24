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
 * ## 措辞为什么是「声明开放」而不是「开放」（批 2 Spec 轴 P1 结论）
 *
 * 设计稿里那句话原本写作「该档位只开放 N 个工具（共 M 个）」。**这个说法在真机上是
 * 假的**：`tool_scope` 数的是**声明的工具面**，而本部署实际注册的工具是另一个集合，
 * 两个方向都会差（最小 harness 实测：`Settings(capabilities="")` + `assemble_wiring` +
 * `build_runtime`，之后数 `registry.list()`）——
 *   - 声明里有、本部署没注册：实测注册数 main **10** / coding **9** /
 *     research_review **3**，而声明是 18/13/8（`research_review` 声明的
 *     `retrieve_knowledge` / `web_search` / `retrieve_memory` / `retrieve_memory_v2` 根本不在 registry 里）
 *     ⇒ "只开放 8 个"是高报。这个数还**不是常量**：同一个 harness 里 websearch 缺
 *     `TAVILY_API_KEY`、multiagent 缺 session_store 时都按 optional 降级缺席，补上就变
 *     ——所以"本部署 = N 个工具"这种说法本身就不稳；
 *   - 注册了、但不在任何声明里：本地 artifact 存储下 `read_artifact` 会被收窄掉，
 *     却不在 `excluded` 里（声明面只声明了 `inspect_artifact`）。
 * 两个数都不是"你现在有多少工具"。所以文案改成**声明口径**（「声明开放 N 个工具
 * （全部档位声明 M 个）」）——它逐字为真，且仍然完成用户要的那件事：让用户看见
 * "选了这个档位，工具面被收窄了"。算真值需要在会话上下文里数收窄前后的 registry
 * （`assembly.py:277-287` 的 `dropped_tools`），属另一张票。
 *
 * 口径（三条都与后端对齐，抄在这里方便复核）：
 *   - 数字来自 `GET /api/agent-profiles` 的 `tool_scope`（后端 `tool_scope_summary`）；
 *   - 后端**没给** `tool_scope`（老部署 / 夹具没带）⇒ 返回 null，不显示——宁可不提示，
 *     也不编一个数；
 *   - `excluded` 为空（该档位没被收窄，例如「通用」）⇒ 也返回 null：
 *     「声明 18 个中开放 18 个」只是噪音，用户没被收窄就没有事实要披露。
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
  /** footer 那一行文字（声明口径，见文件头）。 */
  text: string;
  /** hover/聚焦时的 `title`：**哪个档位** + 被收窄掉的工具名（最多 6 个 + 「…」）。
   *  带档位名是为了让归属无歧义：footer 描述的是**当前生效档位**，而列表里高亮的那
   *  一行可能是别的档位（鼠标移动即高亮），只说「未开放：…」会让人以为是高亮那一行。 */
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
    // 声明口径：这批数**不是**"你现在有 N 个工具"（见文件头）。
    text: `该档位声明开放 ${scope.open} 个工具（全部档位声明 ${scope.total} 个）`,
    // 只列名字、不加解释——设计稿明说"只说事实，不解释原因"（原因属于产品文档，
    // 而这个 tooltip 的位置只够一行）。档位名在前，杜绝"这是高亮那一行的信息"的误读。
    // 「…」前留一个「、」，否则最后一个名字和省略号黏在一起（"forget_memory…"）。
    title: `${entry.display_name}未声明开放：${listed.join('、')}${more ? '、…' : ''}`,
  };
}
