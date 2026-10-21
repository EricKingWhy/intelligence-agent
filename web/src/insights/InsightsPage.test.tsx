// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type * as ApiModule from '../lib/api';
import type { InsightsOverview } from '../lib/api';
import { getContextUsage, getInsightsOverview } from '../lib/api';
import { InsightsPage } from './InsightsPage';

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof ApiModule>('../lib/api');
  return {
    ...actual,
    getInsightsOverview: vi.fn(),
    getContextUsage: vi.fn(),
  };
});

function overview(overrides: Partial<InsightsOverview> = {}): InsightsOverview {
  return {
    kpi: {
      sessions: 2,
      billed_tokens: 1_000,
      cache_hit_rate: 0.25,
      turns: 8,
      tool_runs: 3,
      active_ms: 12_000,
    },
    daily: [
      { date: '2026-10-19', tokens: 400 },
      { date: '2026-10-20', tokens: 600 },
    ],
    heatmap: [
      { date: '2026-10-19', tokens: 400 },
      { date: '2026-10-20', tokens: 600 },
    ],
    composition: {
      messages: 500,
      system_prompt: 200,
      skills: 100,
      tools_system: 100,
      tools_mcp: 50,
      other: 50,
    },
    coverage: { snapshotted_sessions: 1, total_sessions: 2 },
    sessions: [
      {
        session_id: 'alpha',
        title: '修复登录',
        workspace: 'web',
        billed_tokens: 900,
        turns: 6,
        last_active_at: '2026-10-20T10:00:00',
      },
      {
        session_id: 'beta',
        title: '上下文洞察',
        workspace: 'infra',
        billed_tokens: 100,
        turns: 2,
        last_active_at: '2026-10-19T10:00:00',
      },
    ],
    ...overrides,
  };
}

let host: HTMLDivElement;
let root: Root;

async function renderPage(onOpenSession = vi.fn(), onClose = vi.fn()): Promise<void> {
  await act(async () => {
    root.render(<InsightsPage onClose={onClose} onOpenSession={onOpenSession} />);
  });
  const promise = new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });
  await act(async () => {
    await promise;
  });
}

async function clickText(text: string): Promise<void> {
  const button = Array.from(host.querySelectorAll('button'))
    .find((candidate) => candidate.textContent?.trim() === text);
  expect(button, `button ${text}`).toBeTruthy();
  await act(async () => {
    button!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  vi.mocked(getInsightsOverview).mockResolvedValue(overview());
  vi.mocked(getContextUsage).mockResolvedValue({
    estimated: true,
    window_tokens: 200_000,
    used_tokens: 80_000,
    thresholds: { auto_compact: 0.7, hard_guard: 0.85 },
    breakdown: {
      messages: 80_000,
      system_prompt: 0,
      skills: 0,
      other: 0,
      tools: { system: 0, mcp: 0 },
    },
    cache: { state: 'not_collected', reported_calls: 0, total_calls: 0, avg_hit_rate: null },
    state: 'ok',
  });
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

describe('InsightsPage', () => {
  it('renders KPIs, composition coverage, and session cards from the overview payload', async () => {
    await renderPage();
    expect(host.querySelector('.ctx-insights-heading h1')?.textContent).toBe('上下文洞察');
    expect(host.textContent).toContain('1,000');
    expect(host.textContent).toContain('25.0%');
    expect(host.textContent).toContain('覆盖 1/2 个会话');
    expect(host.querySelectorAll('.ctx-insights-session-card')).toHaveLength(2);
    expect(host.textContent).toContain('修复登录');
  });

  it('filters session cards by search text without touching the overview data', async () => {
    await renderPage();
    const input = host.querySelector<HTMLInputElement>('input[type="search"]')!;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    await act(async () => {
      setter?.call(input, '登录');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(host.querySelectorAll('.ctx-insights-session-card')).toHaveLength(1);
    expect(Array.from(host.querySelectorAll('.ctx-insights-session-title'))
      .map((node) => node.textContent)).toEqual(['修复登录']);
    expect(getInsightsOverview).toHaveBeenCalledTimes(1);
  });

  it('sorts by billed tokens and opens a card through the callback', async () => {
    const onOpenSession = vi.fn();
    await renderPage(onOpenSession);
    await clickText('用量');
    const titles = Array.from(host.querySelectorAll('.ctx-insights-session-title'))
      .map((node) => node.textContent);
    expect(titles).toEqual(['修复登录', '上下文洞察']);
    const card = host.querySelector<HTMLButtonElement>('.ctx-insights-session-card')!;
    await act(async () => {
      card.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    });
    expect(onOpenSession).toHaveBeenCalledWith('alpha');
  });

  it('keeps the page shell and shows a retryable error when the overview request fails', async () => {
    vi.mocked(getInsightsOverview).mockRejectedValueOnce(new Error('overview 500'));
    await renderPage();
    expect(host.querySelector('.ctx-insights-heading h1')?.textContent).toBe('上下文洞察');
    expect(host.querySelector('.ctx-insights-back')).toBeTruthy();
    expect(host.querySelector('.ctx-insights-error')?.textContent).toContain('overview 500');
    vi.mocked(getInsightsOverview).mockResolvedValueOnce(overview());
    await clickText('重试');
    const promise = new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });
    await act(async () => {
      await promise;
    });
    expect(host.querySelector('.ctx-insights-error')).toBeNull();
    expect(host.textContent).toContain('1,000');
  });

  it('clears the search on Escape before closing the page', async () => {
    const onClose = vi.fn();
    await renderPage(vi.fn(), onClose);
    const input = host.querySelector<HTMLInputElement>('input[type="search"]')!;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    await act(async () => {
      setter?.call(input, '登录');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(onClose).not.toHaveBeenCalled();
    expect(input.value).toBe('');
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('refetches with the selected range', async () => {
    await renderPage();
    await clickText('近 30 天');
    expect(getInsightsOverview).toHaveBeenLastCalledWith('30d');
  });
});
