// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type * as ApiModule from '../lib/api';
import type { ContextTimeline, FleetPayload } from '../lib/api';
import { getContextTimeline, getSessionFleet } from '../lib/api';
import { SessionInsightsPage } from './SessionInsightsPage';

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof ApiModule>('../lib/api');
  return {
    ...actual,
    getContextTimeline: vi.fn(),
    getSessionFleet: vi.fn(),
  };
});

function timeline(overrides: Partial<ContextTimeline> = {}): ContextTimeline {
  return {
    window: {
      estimated: true,
      window_tokens: 200_000,
      used_tokens: 80_000,
      thresholds: { auto_compact: 0.7, hard_guard: 0.85 },
      breakdown: {
        messages: 50_000,
        system_prompt: 20_000,
        skills: 10_000,
        other: 0,
        tools: { system: 0, mcp: 0 },
      },
      cache: { state: 'ok', reported_calls: 2, total_calls: 2, avg_hit_rate: 0.4 },
      state: 'ok',
    },
    requests: [
      {
        seq: 1,
        time: '2026-10-20T10:00:00',
        prompt_tokens: 100,
        completion_tokens: 20,
        cached_tokens: 40,
        total_tokens: 120,
        duration_ms: 900,
      },
      {
        seq: 2,
        time: '2026-10-20T10:01:00',
        prompt_tokens: 180,
        completion_tokens: 30,
        cached_tokens: 80,
        total_tokens: 210,
        duration_ms: 1_100,
      },
    ],
    events: [
      { seq: 2, time: '2026-10-20T10:00:30', type: 'compaction/start' },
    ],
    ...overrides,
  };
}

function fleet(overrides: Partial<FleetPayload> = {}): FleetPayload {
  return {
    self: {
      session_id: 's1',
      status: 'idle',
      tokens: 80_000,
      last_active_at: '2026-10-20T10:00:00',
    },
    ancestors: [],
    children: [],
    ...overrides,
  };
}

let host: HTMLDivElement;
let root: Root;

async function flush(): Promise<void> {
  const promise = new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });
  await act(async () => {
    await promise;
  });
}

async function renderPage(): Promise<void> {
  await act(async () => {
    root.render(
      <SessionInsightsPage sessionId="s1" onClose={vi.fn()} onOpenSession={vi.fn()} />,
    );
  });
  await flush();
}

async function clickText(text: string): Promise<void> {
  const button = Array.from(host.querySelectorAll('button'))
    .find((candidate) => candidate.textContent?.trim() === text);
  expect(button, `button ${text}`).toBeTruthy();
  await act(async () => {
    button!.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  await flush();
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  vi.mocked(getContextTimeline).mockResolvedValue(timeline());
  vi.mocked(getSessionFleet).mockResolvedValue(fleet());
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

describe('SessionInsightsPage', () => {
  it('renders the window composition, request trend, and context events', async () => {
    await renderPage();
    expect(host.textContent).toContain('当前窗口构成');
    expect(host.textContent).toContain('缓存命中 40.0%');
    expect(host.textContent).toContain('逐请求趋势');
    expect(host.textContent).toContain('压缩开始');
    expect(host.querySelectorAll('.ctx-insights-trend-bar').length).toBeGreaterThan(0);
  });

  it('refetches the timeline when mode and granularity change', async () => {
    await renderPage();
    await clickText('增量');
    expect(getContextTimeline).toHaveBeenLastCalledWith('s1', 'step', 'delta');
    await clickText('按轮');
    expect(getContextTimeline).toHaveBeenLastCalledWith('s1', 'turn', 'delta');
    expect(host.textContent).toContain('逐轮趋势');
    expect(host.querySelector('.ctx-insights-chart-scroll')?.getAttribute('aria-label'))
      .toContain('逐轮 token 趋势');
  });

  it('degrades the context panel without hiding Fleet when the timeline fails', async () => {
    vi.mocked(getContextTimeline).mockRejectedValueOnce(new Error('timeline 500'));
    await renderPage();
    expect(host.querySelector('.ctx-insights-heading h1')?.textContent).toBe('会话上下文');
    expect(host.querySelector('.ctx-insights-error')?.textContent).toContain('timeline 500');
    await clickText('Fleet');
    expect(getSessionFleet).toHaveBeenCalledWith('s1');
    expect(host.textContent).toContain('本会话还没有委派子代理');
    expect(host.querySelector('[aria-label="父代理"]')).toBeNull();
  });

  it('wires the context / Fleet tabs as one roving tab stop with resolvable panels', async () => {
    await renderPage();
    const tabs = host.querySelectorAll<HTMLButtonElement>('[role="tab"]');
    expect(tabs).toHaveLength(2);
    expect(tabs[0].getAttribute('aria-controls')).toBe('ctx-insights-panel-context');
    expect(tabs[1].getAttribute('aria-controls')).toBe('ctx-insights-panel-fleet');
    expect(host.querySelector('#ctx-insights-panel-context')).toBeTruthy();
    expect(host.querySelector('#ctx-insights-panel-fleet')).toBeTruthy();
    expect(tabs[0].tabIndex).toBe(0);
    expect(tabs[1].tabIndex).toBe(-1);

    await act(async () => {
      tabs[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    });
    await flush();
    const after = host.querySelectorAll<HTMLButtonElement>('[role="tab"]');
    expect(after[1].getAttribute('aria-selected')).toBe('true');
    expect(document.activeElement).toBe(after[1]);
    expect(host.querySelector('#ctx-insights-panel-fleet')?.hasAttribute('hidden')).toBe(false);
    expect(getSessionFleet).toHaveBeenCalledWith('s1');
  });

  it('shows usage_only as an explicit lower bound instead of six zero buckets', async () => {
    vi.mocked(getContextTimeline).mockResolvedValueOnce(timeline({
      window: {
        ...timeline().window,
        used_tokens: 42_000,
        breakdown: {
          messages: 0,
          system_prompt: 0,
          skills: 0,
          other: 0,
          tools: { system: 0, mcp: 0 },
        },
        state: 'usage_only',
        usage_source: {
          kind: 'last_call_prompt_tokens',
          calls_with_usage: 1,
          last_prompt_tokens: 42_000,
          last_total_tokens: 42_500,
        },
      },
    }));
    await renderPage();
    expect(host.textContent).toContain('仅用量口径');
    expect(host.textContent).toContain('分类未上报');
    expect(host.querySelectorAll('.ctx-insights-legend-row')).toHaveLength(1);
  });
});
