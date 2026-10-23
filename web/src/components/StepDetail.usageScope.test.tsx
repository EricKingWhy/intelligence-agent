// @vitest-environment jsdom
/** #1524：Inspector 三个计数槽的**作用域/单位**锁。
 *
 *  症状（旧树）：RUN 段「字数」与 MODEL 段「用量 … 字」都绑定 run 级槽
 *  `usage_total`，而该槽**不在 run 边界清零**（#1313 根因的记录在 types.ts /
 *  displayedSessionUsage 注释里）。后果是第二轮还在流式时，两段显示的是
 *  「上一轮权威值 + 本轮流式值」——跨轮累计，正是本票禁止用文案掩盖的那个 bug。
 *
 *  本文件只断言**可观察行为**（DOM 文本），不碰实现：两个 run、有/无 usage、
 *  token 与字符的反控，逐槽核来源与生命周期。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { applyEvent, initConversation, summarizeEvent } from '../lib/projection';
import { EventType } from '../types';
import type { AgentEvent, ConversationState } from '../types';
import { ChatTab } from './StepDetail';

let container: HTMLDivElement | null = null;
let root: Root | null = null;

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  HTMLElement.prototype.scrollIntoView = vi.fn();
});

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  container = null;
  root = null;
});

const conv = (events: AgentEvent[]): ConversationState => {
  let s = initConversation('scope');
  for (const e of events) s = applyEvent(s, e);
  return s;
};

const render = (events: AgentEvent[]) =>
  act(() => root!.render(<ChatTab conversation={conv(events)} tools={[]} />));

const section = (id: string) =>
  container!.querySelector<HTMLElement>(`[data-ov-section="${id}"]`)!;

/** 段内按 key 文本取一行（`—`/数值都在 `.detail-val` 里）。 */
const row = (sectionId: string, key: string): HTMLElement | undefined =>
  Array.from(section(sectionId).querySelectorAll<HTMLElement>('.detail-row')).find(
    (r) => r.querySelector('.detail-key')?.textContent?.trim() === key,
  );

const rowValue = (sectionId: string, key: string): string | undefined =>
  row(sectionId, key)?.querySelector('.detail-val')?.textContent?.trim();

/** 段内所有 key 文案（用来断言「不再出现某标签」）。 */
const keys = (sectionId: string): string[] =>
  Array.from(section(sectionId).querySelectorAll('.detail-key')).map(
    (k) => k.textContent?.trim() ?? '',
  );

// ── 夹具：两个 run ────────────────────────────────────────────────────────────
const ev = (partial: Partial<AgentEvent> & { type: string }): AgentEvent => ({
  data: {}, seq: null, run_id: null, step_id: null, ...partial,
});

const modelCompleted = (runId: string, total: number) => ev({
  type: EventType.MODEL_COMPLETED,
  run_id: runId,
  step_id: 1,
  data: {
    content: 'ok',
    model: 'gpt-x',
    usage: { prompt_tokens: total, completion_tokens: 0, total_tokens: total },
  },
});

const runCompleted = (runId: string, total: number | null) => ev({
  type: EventType.RUN_COMPLETED,
  run_id: runId,
  data: total === null
    ? {}
    : { usage_total: { prompt_tokens: total, completion_tokens: 0, total_tokens: total } },
});

const runStarted = (runId: string) =>
  ev({ type: EventType.RUN_STARTED, run_id: runId, data: {} });

/** run1：流式 600 → 权威 900，已终态；run2 尚未产生 usage。 */
const RUN1_DONE = [runStarted('r1'), modelCompleted('r1', 600), runCompleted('r1', 900)];
/** run1 已终态 + run2 已开始但一次 usage 都还没有。 */
const RUN2_EMPTY = [...RUN1_DONE, runStarted('r2')];
/** run1 已终态 + run2 正在流式（500）。 */
const RUN2_STREAMING = [...RUN2_EMPTY, modelCompleted('r2', 500)];

describe('#1524 RUN 段本轮 tokens：run 边界不跨轮累计', () => {
  it('AC2：run2 开跑但本轮无 usage → 显示「—」，不是上一轮的 900', () => {
    render(RUN2_EMPTY);
    expect(rowValue('run', '本轮 tokens')).toBe('—');
  });

  it('AC2：run2 流式 500 → 显示 500（不是 900+500=1400 的跨轮累计）', () => {
    render(RUN2_STREAMING);
    expect(rowValue('run', '本轮 tokens')).toBe('500');
  });

  it('AC1：run/completed 权威值覆盖本轮流式累计（不叠加）', () => {
    render([...RUN2_STREAMING, runCompleted('r2', 480)]);
    expect(rowValue('run', '本轮 tokens')).toBe('480');
  });

  it('AC1：无 usage 的 run（同一夹具里 usage 全程缺席）→ 「—」，不补 0', () => {
    render([
      runStarted('r0'),
      modelCompleted('r0', 900),
      runCompleted('r0', 900),
      runStarted('r1'),
      ev({ type: EventType.RUN_COMPLETED, run_id: 'r1', data: {} }),
    ]);
    expect(rowValue('run', '本轮 tokens')).toBe('—');
    expect(rowValue('run', '本轮 tokens')).not.toBe('0');
  });

  it('AC1：标签说清单位与作用域——RUN 段不再出现「字数」槽', () => {
    render(RUN2_STREAMING);
    expect(keys('run')).toContain('本轮 tokens');
    expect(keys('run')).not.toContain('字数');
  });
});

describe('#1524 MODEL 段：同源同理，单位是 token 不是字', () => {
  it('AC2：run2 流式 500 → MODEL 段本轮 tokens 是 500，prompt/completion 分解同值', () => {
    render(RUN2_STREAMING);
    const v = rowValue('model', '本轮 tokens');
    expect(v).toContain('500');
    expect(v).not.toContain('1,400');
    expect(v).not.toContain('字');
  });

  it('AC1：MODEL 段不再用「字」标注 token 数', () => {
    render(RUN2_STREAMING);
    expect(keys('model')).not.toContain('用量');
    expect(section('model').textContent).not.toMatch(/\d\s*字/);
  });

  it('AC1：run2 无 usage → MODEL 段本轮 tokens 为「—」，不沿用上一轮数值', () => {
    render(RUN2_EMPTY);
    const v = rowValue('model', '本轮 tokens');
    expect(v === undefined || v === '—').toBe(true);
    expect(section('model').textContent).not.toContain('900');
  });
});

describe('#1524 CONTEXT 段：估算是估算，单位是 token', () => {
  const withCompaction = (tokenEstimate: number) => [
    runStarted('r1'),
    modelCompleted('r1', 900),
    ev({
      type: EventType.CONTEXT_COMPACTED,
      run_id: 'r1',
      data: {
        compacted_turn_count: 5,
        summary_message_count: 1,
        token_estimate: tokenEstimate,
        fallback_used: false,
      },
    }),
  ];

  it('AC1：标签同时表达「估算」与 token 单位，不再叫「字数估算」', () => {
    render(withCompaction(2048));
    expect(keys('context')).not.toContain('字数估算');
    const label = keys('context').find((k) => k.includes('估算'));
    expect(label).toBeDefined();
    expect(label).toContain('token');
  });

  it('AC2：估算值照实渲染（2,048），不被当成实测', () => {
    render(withCompaction(2048));
    expect(section('context').textContent).toMatch(/2[,，]?048/);
    expect(section('context').textContent).toContain('估算');
  });
});

describe('#1524 AC3：字符数与 token 数可辨别（反控）', () => {
  it('同一条模型回复：有 usage 报 token，无 usage 回落字符——两种单位不同名', () => {
    // 中文 4 个字，token 数 12：两个数字必须都能被读懂，且单位名不同。
    const withUsage = summarizeEvent(ev({
      type: EventType.MODEL_COMPLETED,
      data: { content: '你好世界', model: 'gpt-x', usage: { prompt_tokens: 12, completion_tokens: 0, total_tokens: 12 } },
    }));
    // 无 usage 时回落内容字符数（不带 model 才走回落分支，带 model 只剩模型名）。
    const without = summarizeEvent(ev({
      type: EventType.MODEL_COMPLETED,
      data: { content: '你好世界' },
    }));
    expect(withUsage).toContain('12');
    expect(withUsage).not.toContain('字');
    expect(without).toContain('4');
    expect(without).toContain('字符');
    expect(withUsage).not.toBe(without);
  });

  it('RUN_COMPLETED 摘要用 token 单位；压缩摘要同理', () => {
    expect(summarizeEvent(ev({
      type: EventType.RUN_COMPLETED,
      data: { usage_total: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 1801 }, cost_usd: 0.0024 },
    }))).toBe('1801 tok · $0.0024');
    expect(summarizeEvent(ev({
      type: EventType.CONTEXT_COMPACTED,
      data: { compacted_turn_count: 5, token_estimate: 2048 },
    }))).toBe('5 轮 · 2048 tok（估算）');
  });
});
