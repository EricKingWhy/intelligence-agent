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

// #1325 S5：useCompactSession 用真 `SessionError` 做 `reason` 分派——部分 mock
// 必须带上 actual 导出，否则 `instanceof` 在 mock 边界上拿到 undefined。
vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api');
  return {
    ...actual,
    getContextUsage: vi.fn(),
    compactSession: vi.fn(),
    describeSessionError: vi.fn((e: unknown, fallback: string) => {
      const message = (e as Error | null)?.message;
      return message || fallback;
    }),
  };
});

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
    expect(button().textContent).toContain('精简中');
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
      '没有可以精简的早期内容',
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
      '没有可以精简的早期内容（未改动）',
    );
  });
});

describe('ContextUsagePanel「其他」桶细分（#1095）', () => {
  const OTHER_DETAIL = {
    provider_injected: 300,
    runtime_context: 120,
    plan_anchor: 50,
    protected_facts: 20,
    modified_paths: 10,
    pressure_warning: 0,
    progress: 0,
  };

  it('ok 态且后端带 other_detail 时，图例列出全部子项（含 0 子项，不隐藏）', async () => {
    vi.mocked(getContextUsage).mockResolvedValue(
      usage({
        breakdown: {
          messages: 100, system_prompt: 50, skills: 10, other: 500,
          tools: { system: 0, mcp: 0 },
          other_detail: OTHER_DETAIL,
        },
      }),
    );
    await renderPanel();

    const sub = document.body.querySelector('.ctx-usage-legend-sub');
    expect(sub).not.toBeNull();
    const text = sub!.textContent ?? '';
    // 全部子项加法列出（后端 key 对齐：provider 注入/运行期快照/计划锚块/
    // 牢记事项/修改路径/水位警告/进度核对），0 子项也显示。
    for (const label of ['Provider 注入', '运行期快照', '计划锚块', '牢记事项',
      '修改路径', '水位警告', '进度核对']) {
      expect(text).toContain(label);
    }
    expect(text).toContain('300');
  });

  it('other_detail 缺某 key 时该子项跳过渲染（不伪造 0）', async () => {
    vi.mocked(getContextUsage).mockResolvedValue(
      usage({
        breakdown: {
          messages: 100, system_prompt: 50, skills: 10, other: 500,
          tools: { system: 0, mcp: 0 },
          // key 集演进：后端只带 2 个子项，其余 5 个 key 缺席。
          other_detail: { provider_injected: 300, runtime_context: 200 },
        },
      }),
    );
    await renderPanel();

    const text = document.body.querySelector('.ctx-usage-legend-sub')!.textContent ?? '';
    // 在场的子项照常渲染。
    expect(text).toContain('Provider 注入');
    expect(text).toContain('运行期快照');
    // 缺席的 key 不渲染「标签 0」（假零）。
    expect(text).not.toContain('计划锚块');
    expect(text).not.toContain('牢记事项');
    expect(text).not.toContain('修改路径');
    expect(text).not.toContain('水位警告');
    expect(text).not.toContain('进度核对');
  });

  it('旧形状快照（无 other_detail）⇒ 不渲染子项，只显示整块「其他」', async () => {
    await renderPanel();

    expect(document.body.querySelector('.ctx-usage-legend-sub')).toBeNull();
    expect(document.body.querySelector('.ctx-usage-legend')?.textContent).toContain('其他');
  });

  it('usage_only 态不渲染子项（无 other_detail 可言）', async () => {
    vi.mocked(getContextUsage).mockResolvedValue(
      usage({
        state: 'usage_only',
        used_tokens: 54_841,
        usage_source: {
          kind: 'last_call_prompt_tokens',
          calls_with_usage: 1,
          last_prompt_tokens: 54_841,
          last_total_tokens: 61_342,
        },
      }),
    );
    await renderPanel();

    expect(document.body.querySelector('.ctx-usage-legend-sub')).toBeNull();
  });
});
