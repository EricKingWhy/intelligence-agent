/** #199 模型可用性 / 能力位的口径单测。
 *
 * 这两条都是"看不错、但可能说错"的规则：把可用项置灰、把未声明的能力位画成徽标，
 * 界面上都不会报错，只会安静地少说/多说一句。所以规则单独可测。 */

import { describe, expect, it } from 'vitest';
import type { ModelCatalogEntry } from './api';
import {
  UNCONFIGURED_LABEL,
  capabilityBadges,
  isUnavailable,
  providerAvailability,
  reasonLabel,
} from './modelAvailability';

const m = (over: Partial<ModelCatalogEntry> = {}): ModelCatalogEntry => ({
  name: 'x',
  provider: 'p',
  model: 'x',
  default: false,
  ...over,
});

describe('isUnavailable', () => {
  it('只有后端明确说 false 才算不可用', () => {
    expect(isUnavailable(m({ isAvailable: false }))).toBe(true);
    expect(isUnavailable(m({ isAvailable: true }))).toBe(false);
  });

  it('缺字段（旧载荷 / 夹具）不算不可用——那是"没说"，不是"说不可用"', () => {
    expect(isUnavailable(m())).toBe(false);
  });
});

describe('reasonLabel', () => {
  it('已知机器码翻成人话（映射源：provider_store.py:237-244）', () => {
    expect(reasonLabel('missing_api_key')).toBe('未配置 API Key');
    expect(reasonLabel('credential_unavailable')).toBe('凭据不可用');
  });

  it('无原因 → 「未配置」（后端没说原因，那一档就是"没配好"）', () => {
    expect(reasonLabel(null)).toBe(UNCONFIGURED_LABEL);
    expect(reasonLabel('')).toBe(UNCONFIGURED_LABEL);
    expect(reasonLabel(undefined)).toBe(UNCONFIGURED_LABEL);
  });

  it('未知机器码 → 「不可用」，**不**把码原样打给用户、也不冒称「未配置」', () => {
    // 「未配置」是一个**具体诊断**；后端给了一个我不认识的码时，我只知道"用不了"，
    // 不知道是不是没配置（批 2 Spec 轴 P2：两件事不能压成一句话）。
    expect(reasonLabel('some_future_code')).toBe('不可用');
    expect(reasonLabel('some_future_code')).not.toBe(UNCONFIGURED_LABEL);
  });
});

describe('providerAvailability', () => {
  it('全组不可用 → 不可用，并取组内第一条原因', () => {
    const r = providerAvailability([
      m({ isAvailable: false, unavailableReason: 'missing_api_key' }),
      m({ name: 'y', isAvailable: false, unavailableReason: 'missing_api_key' }),
    ]);
    expect(r).toEqual({ unavailable: true, reason: 'missing_api_key' });
  });

  it('只要还有一个可用模型，整组就算可用（不把一个可用项说成不可用）', () => {
    const r = providerAvailability([
      m({ isAvailable: false, unavailableReason: 'missing_api_key' }),
      m({ name: 'y', isAvailable: true }),
    ]);
    expect(r.unavailable).toBe(false);
    expect(r.reason).toBeNull();
  });

  it('不可用但后端没给原因 → reason 为 null（渲染层回落「未配置」，不编）', () => {
    const r = providerAvailability([m({ isAvailable: false })]);
    expect(r).toEqual({ unavailable: true, reason: null });
  });

  it('空组不算不可用（没有条目可置灰）', () => {
    expect(providerAvailability([])).toEqual({ unavailable: false, reason: null });
  });

  it('组内原因不一致时取第一条非空（同 provider 同因，不一致时不编）', () => {
    const r = providerAvailability([
      m({ isAvailable: false }),
      m({ name: 'y', isAvailable: false, unavailableReason: 'credential_unavailable' }),
    ]);
    expect(r.reason).toBe('credential_unavailable');
  });
});

describe('capabilityBadges', () => {
  it('只有声明为 true 才出徽标（顺序稳定：工具 / 视觉 / 思考）', () => {
    const badges = capabilityBadges(
      m({ supportsTools: true, supportsVision: true, supportsReasoningSummary: true }),
    );
    expect(badges.map((b) => b.label)).toEqual(['工具', '视觉', '思考']);
    expect(badges.map((b) => b.key)).toEqual(['tools', 'vision', 'reasoning']);
  });

  it('声明为 false 不出徽标（后端说"没有这个能力"）', () => {
    expect(capabilityBadges(m({ supportsTools: false }))).toEqual([]);
  });

  it('未声明（null / 缺字段）不出徽标——不知道的事不画成"不支持"', () => {
    expect(capabilityBadges(m({ supportsTools: null, supportsVision: null }))).toEqual([]);
    expect(capabilityBadges(m())).toEqual([]);
  });

  it('部分声明时只出声明了的那几个', () => {
    expect(capabilityBadges(m({ supportsVision: true })).map((b) => b.label)).toEqual(['视觉']);
  });
});
