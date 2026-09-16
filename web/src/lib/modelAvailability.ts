/** 模型目录的可用性 / 能力位的**呈现口径**（#199 冻结 AC 的唯一实现点）。
 *
 * 为什么单独成模块：三条规则都是纯函数（`ModelCatalogEntry[]` → 渲染所需的值），
 * vitest 直测（本仓 vitest 是 SSR，两级菜单的内容只能在 playwright 里断言；但
 * "一组是不是不可用""该显示哪句话"不该既依赖后端又依赖浏览器）。
 *
 * 数据来源（全部来自后端，前端零硬编码目录）：
 *   - `is_available` 是**已配置**语义（有凭据 ⇒ true），ADR-0032 D5；
 *   - `unavailable_reason` 是机器码（`provider_store.py:237-244`）。
 */

import type { ModelCatalogEntry } from './api';

/** 一行能力徽标（`label` 就是显示文本，无图标名——图标由渲染层按 key 决定）。 */
export interface CapabilityBadge {
  key: 'tools' | 'vision' | 'reasoning';
  label: string;
}

/** 某些 `unavailable_reason` 机器码的短文案。
 *
 * ⚠ 这是**已知码的翻译表**，不是"可用性判定"：判定只有后端做。未知码一律回落
 * 「未配置」（设计稿 §3 的原话："无 reason 显示「未配置」"）——**绝不把机器码
 * 原样打给用户**（`missing_api_key` 不是给用户看的），也**绝不根据码去猜一个
 * 原因**（后端将来加一个码，这里显示的是一句保守的实话，而不是一句编造的解释）。
 * 新增码时同步这张表；对不上的表现是"文案变保守"，不是"文案变错"。 */
const REASON_LABELS: Record<string, string> = {
  missing_api_key: '未配置 API Key',
  credential_unavailable: '凭据不可用',
};

/** 未配置 / 说不清原因时的统一短文案（设计稿 §3 的回落值）。 */
export const UNCONFIGURED_LABEL = '未配置';

/** 单条模型是否不可用——**唯一判据**：只有后端明确说 `false` 才算。
 *
 * 缺字段（旧载荷 / 测试夹具）是"后端没说"，不是"后端说不可用"；把没说渲染成
 * 置灰，等于替用户判它不可用（比不显示更糟：可用项被劝退）。真载荷经 `getModels`
 * 解析后恒有 `isAvailable`，所以这里只会放过夹具与老部署。 */
export function isUnavailable(m: ModelCatalogEntry): boolean {
  return m.isAvailable === false;
}

/** `unavailable_reason` → 行尾短文案。 */
export function reasonLabel(reason: string | null | undefined): string {
  if (!reason) return UNCONFIGURED_LABEL;
  return REASON_LABELS[reason] ?? UNCONFIGURED_LABEL;
}

/** 一个 provider 组的可用性。
 *
 * **全组都不可用**才算这组不可用：只要组里还有一个可用模型，这组就能用，置灰会
 * 把一个可用项说成不可用（比不置灰更糟）。`reason` 取组内第一条非空原因——
 * `unavailable_reason` 来自 provider 本身（同组同因），组内不一致时至少不会编。 */
export function providerAvailability(items: ModelCatalogEntry[]): {
  unavailable: boolean;
  reason: string | null;
} {
  if (items.length === 0) return { unavailable: false, reason: null };
  const unavailable = items.every(isUnavailable);
  if (!unavailable) return { unavailable: false, reason: null };
  const reason = items.find((m) => m.unavailableReason)?.unavailableReason ?? null;
  return { unavailable: true, reason };
}

/** 二级行的能力徽标：**只显示后端声明为 true 的**。
 *
 * `false` 与 `null`（未声明）都不出徽标——前者是"没有这个能力"，后者是"不知道"，
 * 两种都不该出现一个"×"或一个灰徽标（用户看到灰徽标会当成"不支持"，而事实可能只是
 * 这条目录没声明）。这也是「不猜」：徽标只在能被证实时出现。 */
export function capabilityBadges(m: ModelCatalogEntry): CapabilityBadge[] {
  const badges: CapabilityBadge[] = [];
  if (m.supportsTools === true) badges.push({ key: 'tools', label: '工具' });
  if (m.supportsVision === true) badges.push({ key: 'vision', label: '视觉' });
  if (m.supportsReasoningSummary === true) badges.push({ key: 'reasoning', label: '思考' });
  return badges;
}
