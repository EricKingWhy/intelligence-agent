// @vitest-environment jsdom
/** F-06（#1449）：恢复/中断横幅补两个纯前端动作——「在右栏看缺口」与「忽略」。
 *
 *  横幅本体（`.interrupt-banner`）由投影 `conversation.run_interrupted` 驱动，
 *  事实源仍是 `run/interrupted` 事件；本文件在 App 层钉住三件事：
 *   1. 有中断（且非流式）⇒ 两个按钮逐字渲染；无中断 ⇒ 横幅与按钮都不在；
 *   2. 「在右栏看缺口」⇒ Inspector 打开、focus 落到**最后一个** `run/interrupted`
 *      事件（peek 里就是该事件详情，时间线该行 `aria-current`）；
 *   3. 「忽略」⇒ 横幅消失；同一标识（同一次中断）不重现，新标识（新一次中断）重现。
 *
 *  mock 写法照抄 `App.contextFullRouting.test.tsx`（真 App + 可控 `useSession`）。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { applyEvent, initConversation } from './lib/projection';
import type * as SessionModule from './hooks/useSession';
import type * as ProjectsModule from './hooks/useProjects';
import { EventType, type AgentEvent, type ConversationState } from './types';

const H = vi.hoisted(() => ({
  conv: null as ConversationState | null,
  set: null as unknown as (c: ConversationState) => void,
}));

const NOOP = () => {};

vi.mock('./hooks/useSession', async (importOriginal) => {
  // vi.mock 工厂被提升到 import 之前，静态 import 在这里必然未初始化。
  const actual = await importOriginal<typeof SessionModule>();
  const React = await import('react');
  return {
    ...actual,
    useSession: () => {
      const [conv, setConv] = React.useState(H.conv);
      H.set = setConv;
      return {
        sessions: [],
        selectedId: 's1',
        conversation: conv,
        loadingHistory: false,
        historySkeleton: false,
        streaming: false,
        reconnecting: false,
        error: null,
        sessionsError: null,
        titlesById: {},
        sendFailure: null,
        runFailedBeforePlan: null,
        recoverState: {
          status: 'idle', message: null, conflict: false, repaired: 0,
          terminalRepaired: false, stillUnterminated: false, stillDangling: false,
          pendingDecisions: null,
        },
        selectSession: NOOP,
        submitTask: NOOP,
        sendMessage: NOOP,
        cancelStream: NOOP,
        removeSession: NOOP,
        setArchived: NOOP,
        recover: NOOP,
        submitDecisions: NOOP,
        refreshSessions: NOOP,
        changeModel: NOOP,
        fork: NOOP,
        sendSteer: NOOP,
        flushQueue: NOOP,
        cancelItem: NOOP,
      };
    },
  };
});

vi.mock('./hooks/useProjects', async (importOriginal) => {
  const actual = await importOriginal<typeof ProjectsModule>();
  return {
    ...actual,
    useProjects: () => ({
      projects: [],
      loadError: null,
      refresh: NOOP,
      actions: new Proxy({}, { get: () => NOOP }) as never,
    }),
  };
});

import App from './App';

let seq = 0;
function ev(type: string, runId: string, data: Record<string, unknown> = {}): AgentEvent {
  seq += 1;
  return {
    session_id: 's1',
    time: '2026-10-21T00:00:00Z',
    type,
    seq,
    run_id: runId,
    step_id: 1,
    data,
  };
}

/** 一次完整的中断：run/started → run/interrupted。 */
function interrupted(runId: string, reason: string, seqBase: number): AgentEvent[] {
  return [
    { ...ev(EventType.RUN_STARTED, runId, { turn_index: 1 }), seq: seqBase },
    {
      ...ev(EventType.RUN_INTERRUPTED, runId, { interrupted_seq: seqBase, reason }),
      seq: seqBase + 1,
    },
  ];
}

function twoInterruptions(): ConversationState {
  const events = [
    ...interrupted('run-first', 'first_reason', 1),
    ...interrupted('run-second', 'second_reason', 3),
  ];
  return events.reduce((state, e) => applyEvent(state, e), initConversation('s1'));
}

function buttonByText(text: string): HTMLButtonElement | undefined {
  return Array.from(document.body.querySelectorAll('button')).find(
    (b) => (b.textContent ?? '').trim() === text,
  );
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function mountWith(conv: ConversationState): Promise<HTMLDivElement> {
  H.conv = conv;
  const el = document.createElement('div');
  document.body.appendChild(el);
  container = el;
  root = createRoot(el);
  await act(async () => {
    root!.render(<App />);
  });
  return el;
}

beforeEach(() => {
  seq = 0;
  // React 18+ 的 act 环境标记：jsdom 全局类型里没有这个属性，只能断言一次再写。
  const reactActEnv = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
  reactActEnv.IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: NOOP, removeEventListener: NOOP,
    addListener: NOOP, removeListener: NOOP, dispatchEvent: () => false,
  }));
  vi.stubGlobal('ResizeObserver', class {
    observe = NOOP; unobserve = NOOP; disconnect = NOOP;
  });
  Element.prototype.scrollIntoView = NOOP;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.includes('/api/models')
        ? { models: [{ name: 'm', provider: 'p', model: 'm', default: true }] }
        : url.includes('/api/capabilities')
          ? []
          : {};
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }),
  );
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  if (container) container.remove();
  document.body.innerHTML = '';
  root = null;
  container = null;
  H.conv = null;
  vi.unstubAllGlobals();
});

describe('F-06 中断横幅的两个动作', () => {
  it('有中断且非流式 ⇒ 横幅带「在右栏看缺口」「忽略」两个按钮；无中断 ⇒ 不渲染', async () => {
    await mountWith(twoInterruptions());
    const banner = document.querySelector('.interrupt-banner');
    expect(banner).not.toBeNull();
    expect(banner!.textContent).toContain('上次任务在第 1 步中断');
    expect(buttonByText('在右栏看缺口')).toBeTruthy();
    expect(buttonByText('忽略')).toBeTruthy();

    await act(async () => { root!.unmount(); });
    container!.remove();
    document.body.innerHTML = '';

    await mountWith(initConversation('s1'));
    expect(document.querySelector('.interrupt-banner')).toBeNull();
    expect(buttonByText('在右栏看缺口')).toBeUndefined();
    expect(buttonByText('忽略')).toBeUndefined();
  }, 30_000);

  it('「在右栏看缺口」⇒ Inspector 打开，focus 落到最后一个 run/interrupted 事件', async () => {
    const el = await mountWith(twoInterruptions());
    // jsdom 默认宽 1024 < 1200：Inspector 初始折叠。
    const regions = el.querySelector('.app-regions');
    expect(regions?.className).toContain('inspector-closed');
    expect(el.querySelector('.detail-peek')).toBeNull();

    await act(async () => { buttonByText('在右栏看缺口')!.click(); });

    // 右栏打开（折叠只是 class，不卸载）。
    expect(regions?.className).not.toContain('inspector-closed');
    // peek 呈现的是**最后**那次中断事件本身，不是第一次。
    const peek = el.querySelector('.detail-peek');
    expect(peek).not.toBeNull();
    expect(peek!.hasAttribute('hidden')).toBe(false);
    expect(peek!.textContent).toContain('run/interrupted');
    expect(peek!.textContent).toContain('run-second');
    expect(peek!.textContent).not.toContain('run-first');
    // 时间线（默认 tab）该行被选中。
    const selectedRow = el.querySelector('.timeline-row.sel');
    expect(selectedRow).not.toBeNull();
    expect(selectedRow!.getAttribute('aria-current')).toBe('true');
  }, 30_000);

  it('「忽略」⇒ 横幅消失；同一次中断不重现，新一次中断重新出现', async () => {
    await mountWith(twoInterruptions());
    const banner = document.querySelector('.interrupt-banner');
    expect(banner).not.toBeNull();

    await act(async () => { buttonByText('忽略')!.click(); });
    expect(document.querySelector('.interrupt-banner')).toBeNull();

    // 同一标识（同一次中断）重渲染：不重现。
    await act(async () => { H.set({ ...H.conv! }); });
    expect(document.querySelector('.interrupt-banner')).toBeNull();

    // 标识变化（新的一次中断，run_id 不同）：重新出现。
    const nextEvents = interrupted('run-third', 'third_reason', 1);
    await act(async () => {
      H.set(nextEvents.reduce((state, e) => applyEvent(state, e), initConversation('s1')));
    });
    const again = document.querySelector('.interrupt-banner');
    expect(again).not.toBeNull();
    expect(buttonByText('在右栏看缺口')).toBeTruthy();
    expect(buttonByText('忽略')).toBeTruthy();
  }, 30_000);
});
