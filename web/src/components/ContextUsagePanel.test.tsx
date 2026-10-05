// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  compactSession,
  getContextUsage,
  type ContextUsage,
  type SessionContextCompacted,
} from '../lib/api';
import { ContextUsagePanel } from './ContextUsagePanel';

vi.mock('../lib/api', () => ({
  getContextUsage: vi.fn(),
  compactSession: vi.fn(),
  describeSessionError: vi.fn((e: unknown, fallback: string) => {
    const message = (e as Error | null)?.message;
    return message || fallback;
  }),
}));

function usage(overrides: Partial<ContextUsage> = {}): ContextUsage {
  return {
    estimated: true,
    window_tokens: 200_000,
    used_tokens: 140_000,
    thresholds: { auto_compact: 0.7, hard_guard: 0.85 },
    breakdown: {
      messages: 140_000,
      system_prompt: 0,
      skills: 0,
      other: 0,
      tools: { system: 0, mcp: 0 },
    },
    cache: { state: 'not_collected', reported_calls: 0, total_calls: 0, avg_hit_rate: null },
    state: 'ok',
    ...overrides,
  };
}

function compacted(overrides: Partial<SessionContextCompacted> = {}): SessionContextCompacted {
  return {
    bracket_id: 'brk-1',
    source_seq_start: 12,
    source_seq_end: 88,
    tokens_before: 182_400,
    tokens_after: 41_200,
    compacted_turn_count: 8,
    summary_model: 'main-model',
    ...overrides,
  };
}

let host: HTMLDivElement;
let root: Root;

async function renderPanel(): Promise<void> {
  await act(async () => {
    root.render(<ContextUsagePanel sessionId="s1" open onClose={() => {}} />);
    await Promise.resolve();
  });
}

function button(): HTMLButtonElement {
  return document.body.querySelector<HTMLButtonElement>('.ctx-usage-compact-btn')!;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  vi.mocked(getContextUsage).mockResolvedValue(usage());
  vi.mocked(compactSession).mockResolvedValue(compacted());
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.mocked(getContextUsage).mockReset();
  vi.mocked(compactSession).mockReset();
});

describe('ContextUsagePanel 手动压缩', () => {
  it('shows loading (disabled + spinner) while compacting, then the before/after result', async () => {
    let resolveCompact!: (value: SessionContextCompacted) => void;
    vi.mocked(compactSession).mockImplementation(
      () => new Promise((resolve) => { resolveCompact = resolve; }),
    );
    await renderPanel();

    act(() => button().click());
    expect(button().disabled).toBe(true);
    expect(button().textContent).toContain('压缩中');
    expect(document.body.querySelector('.ctx-usage-spinner')).not.toBeNull();

    await act(async () => {
      resolveCompact(compacted());
      await Promise.resolve();
    });
    expect(compactSession).toHaveBeenCalledWith('s1');
    const result = document.body.querySelector('.ctx-usage-compact-result');
    expect(result?.textContent).toContain('182,400');
    expect(result?.textContent).toContain('41,200');
    expect(button().disabled).toBe(false);
  });

  it('re-fetches context usage after a successful compaction', async () => {
    vi.mocked(getContextUsage)
      .mockResolvedValueOnce(usage())
      .mockResolvedValueOnce(usage({ used_tokens: 41_000 }));
    await renderPanel();
    expect(getContextUsage).toHaveBeenCalledTimes(1);

    await act(async () => {
      button().click();
      await Promise.resolve();
    });
    expect(getContextUsage).toHaveBeenCalledTimes(2);
  });

  it('shows the backend detail inline on failure (no optimistic rewrite)', async () => {
    vi.mocked(compactSession).mockRejectedValue(
      new Error('压缩被拒绝：在途 run 运行中（零改动）'),
    );
    await renderPanel();

    await act(async () => {
      button().click();
      await Promise.resolve();
    });

    const error = document.body.querySelector('.ctx-usage-compact-error');
    expect(error?.textContent).toContain('压缩被拒绝：在途 run 运行中');
    expect(document.body.querySelector('.ctx-usage-compact-result')).toBeNull();
  });

  it('greys out the button with a hint when the water level is too low', async () => {
    vi.mocked(getContextUsage).mockResolvedValue(usage({ used_tokens: 10_000 }));
    await renderPanel();

    expect(button().disabled).toBe(true);
    expect(document.body.querySelector('.ctx-usage-compact-note')?.textContent).toContain(
      '水位过低无需压缩',
    );
    expect(compactSession).not.toHaveBeenCalled();
  });

  it('renders a below-floor result as no-op (unchanged)', async () => {
    vi.mocked(compactSession).mockResolvedValue(
      compacted({ bracket_id: null, compacted_turn_count: 0, tokens_before: 9_000, tokens_after: 9_000 }),
    );
    await renderPanel();

    await act(async () => {
      button().click();
      await Promise.resolve();
    });

    expect(document.body.querySelector('.ctx-usage-compact-result')?.textContent).toContain(
      '水位过低，无需压缩',
    );
  });
});
