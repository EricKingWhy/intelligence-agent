// @vitest-environment jsdom
/** F5（#279）：Inspector 三个长列表（TOOLS / DIFFS / ARTIFACTS）的尾窗。
 *
 *  ## 为什么这个文件用 jsdom，而本仓组件测试默认是 SSR
 *  AC4 要证明「点了出口就能到达**全部**条目」——SSR 渲染不出「点一下」。jsdom 在本仓
 *  已有先例（`Conversation.snapframe.test.tsx` / `App.test.tsx`）。
 *
 *  ## 红证口径（改造前必须失败）
 *  改造前三个列表全量渲染：N=500 时行数就是 500，且**没有任何出口按钮**。所以本文件
 *  的上界断言（≤ 100）与「存在出口」断言在改造前是红的；改造后由尾窗让它们变绿。
 *  行数用**结构类名**数（TOOLS = `.detail-tool-row`；Changes / Artifacts 的每一项渲染
 *  一个 `.detail-section`），与 `stepdetail-list-cost.perf.test.ts` 的节点口径同源。
 *
 *  ## 不测什么
 *  观感（窗口条长什么样、滚动位置）不在单测里锁——那是 e2e 的事（`y-inspector-peek`）。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

import type { ToolCall } from '../types';
import { initConversation } from '../lib/projection';
import { ArtifactsTab, ChangesTab, ChatTab } from './StepDetail';

/** React 18+ 要求测试显式声明 act 环境，否则每个用例刷一条 stderr。 */
(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

/** 该列表有 `n` 项时的工具表（同 `stepdetail-list-cost.perf.test.ts` 的夹具）。 */
function mkTools(n: number, kind: 'plain' | 'diff' | 'artifact'): ToolCall[] {
  return Array.from({ length: n }, (_, i) => {
    const base: ToolCall = {
      tool_call_id: `tc${i}`,
      name: `tool-${i}`,
      args: { path: `src/f${i}.ts` },
      status: 'success',
    };
    if (kind === 'diff') {
      return {
        ...base,
        name: 'edit',
        diff: { before: 'const a = 1;\n', after: 'const a = 2;\n', truncated: false },
      };
    }
    if (kind === 'artifact') {
      return {
        ...base,
        name: 'bash',
        artifact: {
          artifact_id: `artifact-${String(i).padStart(8, '0')}`.padEnd(32, '0'),
          size: 4096,
          mime_type: 'text/plain',
          source_tool: 'bash',
        },
      };
    }
    return base;
  });
}

function render(el: ReactElement): void {
  act(() => {
    root.render(el);
  });
}

const noop = () => {};

/** 出口按钮（复用 Timeline 窗口条的实现，见 `app.css` 的 `.timeline-earlier`）。 */
const earlierButton = () => container.querySelector<HTMLButtonElement>('.timeline-earlier');

function clickEarlier(times = 1): void {
  for (let i = 0; i < times; i++) {
    const btn = earlierButton();
    if (!btn) return;
    act(() => {
      btn.click();
    });
  }
}

const rows = (sel: string) => container.querySelectorAll(sel).length;

const N = 500;
/** 上界：三处窗口都远小于它（TOOLS/DIFFS 50、ARTIFACTS 20），取值只为「明显小于 N」。 */
const CEIL = 100;

describe('F5 尾窗：默认挂载节点数有上限（AC3）', () => {
  it('TOOLS：N=500 时默认行数 ≤ 100（改造前 = 500）', () => {
    render(<ChatTab conversation={initConversation('f5')} tools={mkTools(N, 'plain')} onFocusTool={noop} />);
    const n = rows('.detail-tool-row');
    expect(n).toBeLessThanOrEqual(CEIL);
    expect(n).toBeGreaterThan(0);
  });

  it('DIFFS：N=500 时默认行数 ≤ 100（改造前 = 500）', () => {
    render(<ChangesTab tools={mkTools(N, 'diff')} sessionId="f5" />);
    expect(rows('.detail-section')).toBeLessThanOrEqual(CEIL);
  });

  it('ARTIFACTS：N=500 时默认行数 ≤ 100（改造前 = 500）', () => {
    render(<ArtifactsTab tools={mkTools(N, 'artifact')} sessionId="f5" />);
    expect(rows('.detail-section')).toBeLessThanOrEqual(CEIL);
  });
});

describe('F5 尾窗：出口能到达全部条目（AC4）', () => {
  it('TOOLS：有「加载更早」出口，点到窗口到底时行数 = N', () => {
    render(<ChatTab conversation={initConversation('f5')} tools={mkTools(N, 'plain')} onFocusTool={noop} />);
    expect(earlierButton()).not.toBeNull();
    // 默认端是**最新**：最后一行必须是第 N 个工具（AC5）。
    expect(container.querySelector('.detail-tool-row:last-of-type .detail-tool-name')?.textContent)
      .toBe(`tool-${N - 1}`);
    // 一路点到底 ⇒ 全部 N 条可达。
    for (let i = 0; i < 10 && earlierButton(); i++) clickEarlier();
    expect(earlierButton()).toBeNull();
    expect(rows('.detail-tool-row')).toBe(N);
  });

  it('DIFFS / ARTIFACTS：默认先裁、点出口才涨，最终行数 = N', () => {
    render(<ChangesTab tools={mkTools(N, 'diff')} sessionId="f5" />);
    const beforeDiffs = rows('.detail-section');
    /* `before < N` 才是 AC4 的区分点：只断言「最终 = N」在改造前**也成立**
       （全量渲染本来就是 N），那是空过。 */
    expect(beforeDiffs).toBeLessThan(N);
    clickEarlier();
    expect(rows('.detail-section')).toBeGreaterThan(beforeDiffs);
    for (let i = 0; i < 10 && earlierButton(); i++) clickEarlier();
    expect(rows('.detail-section')).toBe(N);

    act(() => root.unmount());
    root = createRoot(container);
    render(<ArtifactsTab tools={mkTools(N, 'artifact')} sessionId="f5" />);
    const beforeArtifacts = rows('.detail-section');
    expect(beforeArtifacts).toBeLessThan(N);
    clickEarlier();
    expect(rows('.detail-section')).toBeGreaterThan(beforeArtifacts);
    for (let i = 0; i < 10 && earlierButton(); i++) clickEarlier();
    expect(rows('.detail-section')).toBe(N);
  });
});

describe('F5 尾窗：窗口不改变任何一行的存在的意义（AC6 守卫）', () => {
  it('TOOLS：窗口化只裁首段，保留的是**尾部**连续段（无空洞）', () => {
    render(<ChatTab conversation={initConversation('f5')} tools={mkTools(N, 'plain')} onFocusTool={noop} />);
    const names = [...container.querySelectorAll('.detail-tool-name')]
      .map((el) => el.textContent)
      .filter((t): t is string => t !== null && t.startsWith('tool-'));
    expect(names.length).toBeGreaterThan(0);
    // 连续：每一行的序号恰好比上一行大 1（裁的是前缀，不是抽样）。
    const idx = names.map((t) => Number(t.slice('tool-'.length)));
    expect(idx).toEqual(idx.map((_, i) => idx[0] + i));
    expect(idx[idx.length - 1]).toBe(N - 1);
  });
});
