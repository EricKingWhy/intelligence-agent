/** BudgetPicker（#536 单入口预算面板）——SSR 渲染契约。
 *
 *  本仓没有 jsdom：面板内容若藏在 Radix Portal 里 SSR 断不了（同权限 pill 的分工，
 *  见 Composer.test.tsx 头注）——所以面板主体抽成独立导出的 `BudgetPickerPanel`
 *  （纯 props 渲染，Portal 壳之外），这里直接渲染它锁：
 *   - 三维控件的 aria-label **沿用 #426 旧名**（turns/datetime 输入与 tokens 自定义
 *     输入框）——e2e/budget-entry.spec.ts 的定位器与既有 e2e 资产不因换壳而作废；
 *   - 未接 onChange 的维不渲染其控件（不渲染吞输入的死框，#426 语义原样）；
 *   - 预览行（deadlinePreview）与过期 --danger；
 *   - tokens 档位命中 → OptionPicker trigger 显示档位标签（500k），自定义 → 「自定义」。 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { BudgetPicker, BudgetPickerPanel } from './BudgetPicker';

const noop = () => {};
const html = (element: React.ReactElement): string =>
  renderToString(element).replaceAll('<!-- -->', '');

// 固定「现在」：本地 2026-10-02 13:30（预览/过期的确定性锚点）。
const NOW = new Date('2026-10-02T13:30:00');

describe('BudgetPickerPanel — 三维控件（#536）', () => {
  const full = {
    turns: '',
    onTurnsChange: noop,
    tokens: '',
    onTokensChange: noop,
    deadline: '',
    onDeadlineChange: noop,
    now: NOW,
  };

  it('三维全接 → turns 输入 / tokens 档位 trigger / datetime 高级路径全在场（aria-label 沿用旧名）', () => {
    const out = html(createElement(BudgetPickerPanel, full));
    expect(out).toContain('aria-label="预算上限（Agent turns，留空为默认）"');
    expect(out).toContain('aria-label="预算 tokens 档位"');
    expect(out).toContain('aria-label="预算截止时间（留空为不设）"');
    expect(out).toContain('type="datetime-local"');
  });

  it('未接 onChange 的维不渲染其控件（不渲染吞输入的死框，#426 语义原样）', () => {
    const out = html(createElement(BudgetPickerPanel, { ...full, onTokensChange: undefined, onDeadlineChange: undefined }));
    expect(out).toContain('aria-label="预算上限（Agent turns，留空为默认）"');
    expect(out).not.toContain('aria-label="预算 tokens 档位"');
    expect(out).not.toContain('aria-label="预算截止时间（留空为不设）"');
  });

  it('tokens 草稿命中档位 → 档位 trigger 显示紧凑标签（500k）', () => {
    const out = html(createElement(BudgetPickerPanel, { ...full, tokens: '500000' }));
    expect(out).toContain('aria-label="预算 tokens 档位"');
    expect(out).toContain('>500k<');
  });

  it('tokens 自定义草稿（300000）→ trigger 显示「自定义」', () => {
    const out = html(createElement(BudgetPickerPanel, { ...full, tokens: '300000' }));
    expect(out).toContain('>自定义<');
  });

  it('tokens 草稿为空 → 档位 trigger 显示 placeholder（tokens 上限）', () => {
    const out = html(createElement(BudgetPickerPanel, { ...full, tokens: '' }));
    expect(out).toContain('>tokens 上限<');
  });

  it('deadline 时长草稿（2h）→ 时长档 trigger 显示「2 小时」', () => {
    const out = html(createElement(BudgetPickerPanel, { ...full, deadline: '2h' }));
    expect(out).toContain('>2 小时<');
  });
});

describe('BudgetPickerPanel — 截止预览行（#536 §2.2）', () => {
  const full = {
    turns: '', onTurnsChange: noop,
    tokens: '', onTokensChange: noop,
    deadline: '', onDeadlineChange: noop,
    now: NOW,
  };

  it('deadline 为空 → 无预览行（不占位）', () => {
    const out = html(createElement(BudgetPickerPanel, full));
    expect(out).not.toContain('composer-budget-preview');
  });

  it('时长草稿 → 「≈ MM-DD HH:mm 截止（2 小时后）」', () => {
    const out = html(createElement(BudgetPickerPanel, { ...full, deadline: '2h' }));
    expect(out).toMatch(/≈ \d{2}-\d{2} \d{2}:\d{2} 截止（2 小时后）/);
    expect(out).not.toContain('composer-budget-preview--danger');
  });

  it('datetime 高级路径 → 「≈ … 截止」（无相对语）', () => {
    const out = html(createElement(BudgetPickerPanel, { ...full, deadline: '2026-10-02T15:30' }));
    expect(out).toMatch(/≈ \d{2}-\d{2} \d{2}:\d{2} 截止<\/span>/);
  });

  it('已过期（datetime 早于 now）→ 「已过期」 + --danger 修饰类', () => {
    const out = html(createElement(BudgetPickerPanel, { ...full, deadline: '2026-10-02T09:00' }));
    expect(out).toContain('已过期');
    expect(out).toContain('composer-budget-preview--danger');
  });
});

describe('BudgetPicker — 单入口 trigger（#536 §2.3/§2.5）', () => {
  const full = {
    turns: '', onTurnsChange: noop,
    tokens: '', onTokensChange: noop,
    deadline: '', onDeadlineChange: noop,
  };

  it('trigger aria-label="预算" + 全空摘要「默认」', () => {
    const out = html(createElement(BudgetPicker, full));
    expect(out).toContain('aria-label="预算"');
    expect(out).toContain('>默认<');
  });

  it('草稿有值 → 摘要随 drafts 变化（turns 5 · 500k · 2h）', () => {
    const out = html(
      createElement(BudgetPicker, { ...full, turns: '5', tokens: '500000', deadline: '2h' }),
    );
    expect(out).toContain('>turns 5 · 500k · 2h<');
  });

  it('disabled → aria-disabled="true"（SSR 可见，同 OptionPicker 契约）', () => {
    const out = html(createElement(BudgetPicker, { ...full, disabled: true }));
    expect(out).toContain('aria-disabled="true"');
  });
});
