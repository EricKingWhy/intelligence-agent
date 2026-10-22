/** #1429 验收：中间对话区「一次渲染吐出多少 HTML / 花多少时间」的复现探针。
 *
 * 口径与 `stepdetail-list-cost.perf.test.ts` 同源（`renderToString` + 预热 10 + n=50 取中位数），
 * fixture 写死为 **N=200 / 500 个 chain 节点**，且按「同类连续 3 个 → 换 kind」排布，
 * 即**每 3 个节点一个分组边界**（含单成员尾组）。票面要求：施工后单次渲染中位数
 * ≤ 基线 +20%。
 *
 * 为什么把 `useVirtualizer` 换成"全部可见"的假实现：真实虚拟化在 SSR 下没有滚动容器
 * ⇒ `getVirtualItems()` 恒为空，量到的是一棵空树。这里不测虚拟化（F2/#272 另有守卫），
 * 只测「N 个节点真的渲染出来」的成本。
 *
 * 本文件与树无关：基线（`5a23f44f2`，无分组）与施工树都能跑同一份 fixture 与同一个
 * `Conversation` 入口，数字可直接对账。
 *
 * 默认车道已排除 `*.perf.test.ts`。跑法：
 *   node node_modules/vitest/vitest.mjs run -c vitest.perf.config.ts \
 *     src/components/conversation-chain.perf.test.ts
 */
import { describe, expect, it, vi } from 'vitest';
import type * as VirtualModule from '@tanstack/react-virtual';

vi.mock('@tanstack/react-virtual', async (importOriginal) => {
  const actual = await importOriginal<typeof VirtualModule>();
  return {
    ...actual,
    useVirtualizer: (opts: { count: number }) => ({
      getVirtualItems: () =>
        Array.from({ length: opts.count }, (_, i) => ({
          index: i,
          key: i,
          start: i * 240,
          end: i * 240 + 240,
          size: 240,
          lane: 0,
        })),
      getTotalSize: () => opts.count * 240,
      measureElement: () => {},
      scrollToIndex: () => {},
      scrollToOffset: () => {},
    }),
  };
});

import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import type { AgentEvent, ConversationState, ToolCall } from '../types';
import { initConversation, projectHistory } from '../lib/projection';
import { useDisclosure, useReasoningDisclosure } from '../lib/disclosure';
import type { TraceDensity } from '../lib/density';
import { Conversation } from './Conversation';

const SID = 'perf-1429';

/** 每 3 个同 kind 节点换一次 kind ⇒ 分组边界密集（每组 3 个，末组按余数）。 */
const KINDS = ['read', 'bash', 'edit', 'mcp__fs__stat'] as const;

function buildEvents(n: number): AgentEvent[] {
  const events: AgentEvent[] = [];
  let seq = 0;
  const push = (type: string, data: Record<string, unknown>, extra: Record<string, unknown> = {}) => {
    seq += 1;
    events.push({ type, data, seq, session_id: SID, run_id: 'r-1', step_id: 1, ...extra } as AgentEvent);
  };
  push('user/message', { content: '跑一个长任务' });
  push('run/started', { turn_index: 1 }, { time: '2026-10-20T00:00:00Z' });
  for (let i = 0; i < n; i++) {
    const name = KINDS[Math.floor(i / 3) % KINDS.length];
    const id = `tc-${i}`;
    push('tool/call', { tool_call_id: id, tool_name: name, args: { path: `src/f${i}.ts` } });
    push('tool/result', {
      tool_call_id: id,
      content: JSON.stringify({ ok: true, message: '', data: { output: `line ${i}\n` } }),
    });
  }
  return events;
}

/** 投影结果按 N 缓存：`Host` 在计时区内渲染，投影本身是另一件事（A 轴 P2）。
 *  两棵树用同一份缓存策略，所以口径仍然对等——只是把 `projectHistory` 从
 *  「单次渲染中位数」里拿掉。 */
const convCache = new Map<number, ConversationState>();

function conversationOf(n: number) {
  let conv = convCache.get(n);
  if (!conv) {
    conv = projectHistory(SID, buildEvents(n));
    convCache.set(n, conv);
  }
  return conv;
}

const noop = () => {};

/** 宿主照抄 App.tsx 的 props 契约（真 hook，SSR 下无副作用）。 */
function Host({ n }: { n: number }) {
  const disclosure = useDisclosure(SID);
  const reasoningDisclosure = useReasoningDisclosure(SID, 'balanced' as TraceDensity);
  return createElement(Conversation, {
    conversation: conversationOf(n),
    loadingHistory: false,
    density: 'balanced' as TraceDensity,
    disclosure,
    reasoningDisclosure,
    jumpRequest: null,
    onPresetTask: noop,
    onFocusTool: noop as (t: ToolCall) => void,
    onOpenSession: noop,
    onInspectChild: noop,
    onFork: noop,
    onEditTurn: noop,
    goneApprovalIds: new Set<string>(),
    onApprovalGone: noop,
  });
}

function bench(label: string, fn: () => string, reps = 50): { median: number; bytes: number } {
  let html = '';
  for (let i = 0; i < 10; i++) html = fn();
  const ts: number[] = [];
  for (let i = 0; i < reps; i++) {
    const t0 = performance.now();
    fn();
    ts.push(performance.now() - t0);
  }
  ts.sort((a, b) => a - b);
  const median = ts[reps >> 1];
  console.log(`  ${label.padEnd(30)} median=${median.toFixed(2)}ms  bytes=${html.length}  (n=${reps})`);
  return { median, bytes: html.length };
}

describe('#1429 中间对话区渲染成本（N=200 / 500 chain 节点）', () => {
  it('单次渲染中位数 + 线性度（拦 O(N²) 回潮）', { timeout: 240_000 }, () => {
    // 先跑一遍 200 预热 V8 的代码路径，再量 500——两档互不污染。
    const small = bench('N=200（含分组边界）', () => renderToString(createElement(Host, { n: 200 })));
    const large = bench('N=500（含分组边界）', () => renderToString(createElement(Host, { n: 500 })));

    // 节点数 ×2.5，成本不该出现超线性爆炸（O(N²) 回潮时 ≈6.25×）。留 4× 余量给抖动。
    expect(large.median).toBeLessThan(small.median * 4 + 5);
    // 绝对预算：500 节点的静态渲染 1s 已是机器无关的"明显劣化"线。
    expect(large.median).toBeLessThan(1000);
    // 节点真的渲染出来了（不是空树）：每个节点至少吐一次 act-node。
    expect((large.bytes - small.bytes) / 300).toBeGreaterThan(200);
  });

  it('fixture 自检：N=200 时工具节点数 = 200', () => {
    const conv = conversationOf(200);
    const tools = conv.turns.flatMap((t) => t.tools);
    expect(tools).toHaveLength(200);
    expect(tools[0].status).toBe('success');
    // 分组边界存在：kind 序列每 3 个一换。
    expect(new Set(tools.map((t) => t.name)).size).toBe(4);
    // 未投影前不该有事件顺序丢失
    expect(initConversation(SID).turns).toHaveLength(0);
  });
});
