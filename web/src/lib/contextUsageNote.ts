/** 看板 `usage_only` 那句说明的文字（#212 审查项）。
 *
 * 为什么单独成模块：它是**纯函数**（`ContextUsage` → 一句话），vitest 直测（与
 * `agentProfileScope.ts`/`modelAvailability.ts` 同一条理由：本仓 vitest 是 SSR，
 * 面板内容靠 e2e 断言；"该说什么话"不该既依赖后端又依赖浏览器）。放在组件文件里
 * 导出会触发 `react(only-export-components)`（Fast refresh 只在文件只导出组件时成立）。
 *
 * 为什么必须看 `kind` 而不是看 `state`：这句话宣称的是"总数取自哪里"，而 `kind`
 * 正是后端对这件事的声明。写死成"输入规模"的话，后端将来把取值换成 `total_tokens`
 * （设计稿 §3.4 明说只改一行取值、不动契约形状）时，界面会继续宣称一个**假的**
 * 取数来源，而 tsc / vitest / e2e 全绿（e2e 夹具是写死的）。未知码退中性说法：
 * 只说"取自最近一次调用的用量上报"，不替后端解释成哪一种。 */

import type { ContextUsage } from './api';

/** 分类缺席时把"为什么"也写出来（诚实约束）：否则「未分类 100%」看起来像我们分类
 *  失败，而不是"分类这一次拿不到"。数字全部来自后端，前端不推算（不变量 #22）。 */
export function usageOnlyNote(usage: ContextUsage): string {
  const source = usage.usage_source;
  const why =
    source?.kind === 'last_call_prompt_tokens'
      ? '总数取自最近一次调用的输入规模（窗口占用下界）'
      : '总数取自最近一次调用的用量上报';
  return source
    ? `分类未采集：${why}，本会话 ${source.calls_with_usage} 次调用有用量上报`
    : `分类未采集：${why}`;
}
