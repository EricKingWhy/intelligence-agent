// @vitest-environment jsdom
/** #1197 项 5B：「上下文窗口」挡位选择器（效果图 01）。
 *
 *  三条硬约束（不是样式偏好，是票面/仓库规则）：
 *  1. 挡位清单**只**来自后端 `tiers`，前端不硬编码副本（后端加一挡前端自动多一挡）。
 *  2. 阈值是后端算出的**绝对 token 数**原样显示——前端不拿比例自己乘（不变量 #22）。
 *  3. 点击即 PATCH；失败**不乐观改写**选中态（就地显示后端 detail，同 ctx-usage 手动压缩口径）。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getContextSettings, setContextWindow, SessionError, type ContextSettings } from '../lib/api';
import { ContextWindowPanel } from './ContextWindowPanel';

vi.mock('../lib/api', async (orig) => ({
  ...(await orig<typeof import('../lib/api')>()),
  getContextSettings: vi.fn(),
  setContextWindow: vi.fn(),
}));

function settings(overrides: Partial<ContextSettings> = {}): ContextSettings {
  return {
    session_id: 's1',
    context_window: 200_000,
    configured_default: 200_000,
    tiers: [100_000, 200_000, 256_000, 300_000, 500_000],
    thresholds: { auto_compact: 140_000, hard_guard: 170_000 },
    ...overrides,
  };
}

let host: HTMLDivElement;
let root: Root;

async function renderPanel(): Promise<void> {
  await act(async () => {
    root.render(<ContextWindowPanel sessionId="s1" open onClose={() => {}} />);
    await Promise.resolve();
  });
}

function radios(): HTMLButtonElement[] {
  return Array.from(document.body.querySelectorAll<HTMLButtonElement>('.ctx-window-choice'));
}
function checked(): string {
  return radios().find((r) => r.getAttribute('aria-checked') === 'true')?.textContent ?? '';
}
function threshold(label: string): string {
  const row = Array.from(document.body.querySelectorAll('.ctx-window-threshold')).find((el) =>
    el.textContent?.startsWith(label),
  );
  return row?.querySelector('strong')?.textContent ?? '';
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  vi.mocked(getContextSettings).mockResolvedValue(settings());
  vi.mocked(setContextWindow).mockResolvedValue(settings({ context_window: 500_000 }));
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.mocked(getContextSettings).mockReset();
  vi.mocked(setContextWindow).mockReset();
});

describe('ContextWindowPanel 挡位选择器（效果图 01）', () => {
  it('挡位渲染自后端 tiers（不是硬编码），当前挡位 aria-checked=true', async () => {
    vi.mocked(getContextSettings).mockResolvedValue(
      settings({ tiers: [50_000, 200_000], context_window: 50_000 }),
    );
    await renderPanel();

    expect(getContextSettings).toHaveBeenCalledWith('s1');
    expect(radios().map((r) => r.textContent)).toEqual(['50K', '200K']);
    expect(checked()).toBe('50K');
    expect(document.body.querySelector('.ctx-window-badge')?.textContent).toContain('50K 当前生效');
  });

  it('阈值与默认挡位都是后端值原样显示（前端不乘比例、不硬编码 200K）', async () => {
    vi.mocked(getContextSettings).mockResolvedValue(
      settings({
        context_window: 300_000,
        configured_default: 256_000,
        thresholds: { auto_compact: 210_000, hard_guard: 255_000 },
      }),
    );
    await renderPanel();

    expect(threshold('开始自动精简')).toBe('210,000');
    expect(threshold('硬上限')).toBe('255,000');
    // 「恢复默认」的落点是 configured_default（256K），不是写死的 200K。
    expect(document.body.querySelector('.ctx-window-reset')?.textContent).toContain('恢复 256K');
  });

  it('点另一挡 → PATCH 该 token 数，回执即新选中态', async () => {
    await renderPanel();
    expect(checked()).toBe('200K');

    await act(async () => {
      radios()[4].click();
      await Promise.resolve();
    });

    expect(setContextWindow).toHaveBeenCalledWith('s1', 500_000);
    expect(checked()).toBe('500K');
    expect(document.body.querySelector('.ctx-window-badge')?.textContent).toContain('500K 当前生效');
  });

  it('PATCH 失败（422 非法挡位）→ 选中态不动，就地显示后端 detail', async () => {
    vi.mocked(setContextWindow).mockRejectedValue(
      new SessionError(422, 'context_window 必须是 [100000, 200000] 之一，得到 12345'),
    );
    await renderPanel();

    await act(async () => {
      radios()[4].click();
      await Promise.resolve();
    });

    // 零改写：仍停在 200K（后端 422 也是零写入，界面与事实一致）。
    expect(checked()).toBe('200K');
    expect(document.body.querySelector('.ctx-window-error')?.textContent).toContain('得到 12345');
  });

  it('「恢复默认」点回 configured_default', async () => {
    vi.mocked(getContextSettings).mockResolvedValue(settings({ context_window: 500_000 }));
    vi.mocked(setContextWindow).mockResolvedValue(settings());
    await renderPanel();

    await act(async () => {
      document.body.querySelector<HTMLButtonElement>('.ctx-window-reset')!.click();
      await Promise.resolve();
    });

    expect(setContextWindow).toHaveBeenCalledWith('s1', 200_000);
    expect(checked()).toBe('200K');
  });

  it('读设置失败 → 显示错误，不渲染挡位（不编一个假清单）', async () => {
    vi.mocked(getContextSettings).mockRejectedValue(new SessionError(404, 'session not found'));
    await renderPanel();

    expect(document.body.querySelector('.ctx-window-error')?.textContent).toContain(
      'session not found',
    );
    expect(radios()).toHaveLength(0);
  });
});
