// @vitest-environment jsdom
/** #1256 SubagentRoster / SubagentSplitPane 行为契约。
 *
 *  钉四件事（issue §5.1/§5.2/§6 AC）：
 *   ① 纯展示口径：token 缩写 / 诚实标注（估算·无数据）/ 耗时四态；
 *   ② 无子智能体不渲染入口（AC8）；
 *   ③ 行主体点击 → onOpenSession（主视图），chevron → onOpenSplit（右栏），互不串道；
 *   ④ 分栏错误态与关闭。
 *
 *  组件间不 mock Conversation——分栏就是要复用真渲染器（不建第二套真相）。
 */

import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  SubagentRoster,
  SubagentSplitPane,
  durationText,
  formatTokenCount,
  tokenText,
} from './SubagentRoster';
import type { DelegationEntry } from '../lib/api';

const NOOP = () => {};

function entry(over: Partial<DelegationEntry> = {}): DelegationEntry {
  return {
    child_session_id: 'c1', target: 'research_review', task: '审一下',
    status: 'completed', started_at: '2026-10-16T00:00:00.000Z',
    completed_at: '2026-10-16T00:12:18.000Z',
    used_tokens: 12842, usage_state: 'ok', ...over,
  };
}

describe('#1256 token 缩写（dsh 口径）', () => {
  it('≥1M → x.xM；≥1000 → x.xK；否则原样', () => {
    expect(formatTokenCount(999)).toBe('999');
    expect(formatTokenCount(1000)).toBe('1.0K');
    expect(formatTokenCount(12_842)).toBe('12.8K');
    expect(formatTokenCount(1_000_000)).toBe('1.0M');
    expect(formatTokenCount(1_234_567)).toBe('1.2M');
  });

  it('非有限 / 负值 → 空串（不编数字）', () => {
    expect(formatTokenCount(-1)).toBe('');
    expect(formatTokenCount(NaN)).toBe('');
    expect(formatTokenCount(Infinity)).toBe('');
  });
});

describe('#1256 tokenText — 诚实口径（AC6）', () => {
  it('ok → 精确值，无标注', () => {
    expect(tokenText(entry({ usage_state: 'ok', used_tokens: 12_842 }))).toBe('12.8K tok');
  });

  it('usage_only → 标「估算」（事件流下界，不当下确值卖）', () => {
    expect(tokenText(entry({ usage_state: 'usage_only', used_tokens: 12_842 }))).toBe('12.8K tok · 估算');
  });

  it('no_data → 「无数据」（不显 0 tok 假象）', () => {
    expect(tokenText(entry({ usage_state: 'no_data', used_tokens: 0 }))).toBe('无数据');
  });
});

describe('#1256 durationText — 四态耗时', () => {
  const NOW = new Date('2026-10-16T00:12:18.000Z').getTime();

  it('completed → 起点→终点「耗时」', () => {
    expect(durationText(entry(), NOW)).toBe('耗时 12分18秒');
  });

  it('running → 起点→now「运行」', () => {
    const e = entry({ status: 'running', completed_at: null, started_at: '2026-10-16T00:00:00.000Z' });
    expect(durationText(e, NOW)).toBe('运行 12分18秒');
  });

  it('缺 started_at → null（不显数字）', () => {
    expect(durationText(entry({ started_at: null }), NOW)).toBeNull();
  });

  it('时钟倒挂（end < start）→ null（与 formatCoarseDuration 同口径）', () => {
    const e = entry({ started_at: '2026-10-16T00:12:18.000Z', completed_at: '2026-10-16T00:00:00.000Z' });
    expect(durationText(e, NOW)).toBeNull();
  });
});

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: NOOP, removeEventListener: NOOP,
    addListener: NOOP, removeListener: NOOP, dispatchEvent: () => false,
  }));
  vi.stubGlobal('ResizeObserver', class { observe = NOOP; unobserve = NOOP; disconnect = NOOP; });
  Element.prototype.scrollIntoView = NOOP;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

function render(node: ReactNode) {
  act(() => { root.render(node); });
}

describe('#1256 SubagentRoster', () => {
  it('无子智能体 → 不渲染入口（AC8：无证据不渲染）', () => {
    render(<SubagentRoster delegations={[]} onOpenSession={NOOP} onOpenSplit={NOOP} />);
    expect(host.querySelector('.agent-trigger')).toBeNull();
    expect(host.textContent).toBe('');
  });

  it('有子智能体 → 触发器显数量，running 显徽标', () => {
    render(
      <SubagentRoster
        delegations={[entry({ child_session_id: 'c1', status: 'running' }), entry({ child_session_id: 'c2' })]}
        onOpenSession={NOOP}
        onOpenSplit={NOOP}
      />,
    );
    expect(host.querySelector('.roster-label')?.textContent).toBe('2 个子智能体');
    expect(host.querySelector('.roster-running-badge')?.textContent).toBe('1');
    expect(host.querySelector('.roster')).toBeNull(); // 未展开
  });

  it('点击触发器 → 展开 roster 行，逐行显名/token/状态', () => {
    render(<SubagentRoster delegations={[entry()]} onOpenSession={NOOP} onOpenSplit={NOOP} />);
    act(() => { host.querySelector<HTMLButtonElement>('.agent-trigger')!.click(); });
    expect(host.querySelector('.roster')).not.toBeNull();
    expect(host.querySelector('.agent-row')).not.toBeNull();
    expect(host.querySelector('.agent-title')?.textContent).toBe('research_review');
    expect(host.querySelector('.agent-stats')?.textContent).toContain('12.8K tok');
    expect(host.querySelector('.agent-stats')?.textContent).toContain('完成');
  });

  it('行主体点击 → onOpenSession（主视图），不触发 onOpenSplit', () => {
    const onOpenSession = vi.fn();
    const onOpenSplit = vi.fn();
    render(<SubagentRoster delegations={[entry()]} onOpenSession={onOpenSession} onOpenSplit={onOpenSplit} />);
    act(() => { host.querySelector<HTMLButtonElement>('.agent-trigger')!.click(); });
    act(() => { host.querySelector<HTMLElement>('.agent-row')!.click(); });
    expect(onOpenSession).toHaveBeenCalledWith('c1');
    expect(onOpenSplit).not.toHaveBeenCalled();
  });

  it('chevron 点击 → onOpenSplit，不触发 onOpenSession（不串道）', () => {
    const onOpenSession = vi.fn();
    const onOpenSplit = vi.fn();
    render(<SubagentRoster delegations={[entry()]} onOpenSession={onOpenSession} onOpenSplit={onOpenSplit} />);
    act(() => { host.querySelector<HTMLButtonElement>('.agent-trigger')!.click(); });
    act(() => { host.querySelector<HTMLButtonElement>('.row-arrow')!.click(); });
    expect(onOpenSplit).toHaveBeenCalledWith({ childSessionId: 'c1', target: 'research_review' });
    expect(onOpenSession).not.toHaveBeenCalled();
  });
});

describe('#1256 SubagentSplitPane', () => {
  it('加载失败 → 显错误行，不渲染 Conversation 正文', () => {
    render(
      <SubagentSplitPane
        child={{ childSessionId: 'c1', target: 'research_review' }}
        conversation={null}
        error="网络错误"
        density="balanced"
        onClose={NOOP}
      />,
    );
    expect(host.querySelector('.subagent-split-status')?.textContent).toContain('子会话加载失败');
    expect(host.querySelector('.subagent-split-status')?.textContent).toContain('网络错误');
  });

  it('正常 → 分栏带 aria-label 与关闭按钮（父会话保持不动）', () => {
    const onClose = vi.fn();
    render(
      <SubagentSplitPane
        child={{ childSessionId: 'abcdef123456', target: 'research_review' }}
        conversation={null}
        error={null}
        density="balanced"
        onClose={onClose}
      />,
    );
    expect(host.querySelector('aside.subagent-split-pane')?.getAttribute('aria-label')).toContain('research_review');
    expect(host.querySelector('.split-subtitle')?.textContent).toBe('abcdef12');
    act(() => { host.querySelector<HTMLButtonElement>('.close-pane')!.click(); });
    expect(onClose).toHaveBeenCalled();
  });
});
