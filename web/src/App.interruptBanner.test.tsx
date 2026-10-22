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
 *
 *  超时：本文件每条用例显式 `30_000`。默认 5s 在本机 2 核高负载（并行 agent 施工）
 *  下会假红——单文件实测墙钟 45–82s，失败点在 `mount()` 附近而不是断言；这里没有
 *  性能断言，30s 只是给真实渲染留出墙钟。兄弟文件不设超时是默认体例，本文件按
 *  `#1449` 审查 P4-5 注明差异原因。
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

/** 直接构造一条 run/interrupted（不带 run/started）：只关心横幅与标识。 */
function singleInterruption(
  seqNo: number,
  runId: string | null,
  sessionId = 's1',
): ConversationState {
  return applyEvent(
    initConversation(sessionId),
    {
      session_id: sessionId,
      time: '2026-10-21T00:00:00Z',
      type: EventType.RUN_INTERRUPTED,
      seq: seqNo,
      run_id: runId,
      step_id: 1,
      data: { interrupted_seq: seqNo - 1, reason: 'anonymous' },
    },
  );
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
    // S-P3-2：横幅动作走专用类，不再命中全局 `.recover-btn`（否则「崩溃 + 中断」
    // 同屏时 e2e 的严格模式 `.recover-btn` locator 会双匹配）。
    expect(banner!.querySelectorAll('.recover-btn')).toHaveLength(0);
    expect(banner!.querySelectorAll('.interrupt-action')).toHaveLength(2);

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
    // 时间线（默认 tab）该行被选中——并且必须是中断事件本身，不是任意一行。
    const selectedRow = el.querySelector('.timeline-row.sel');
    expect(selectedRow).not.toBeNull();
    expect(selectedRow!.getAttribute('aria-current')).toBe('true');
    expect(selectedRow!.textContent).toContain('run/interrupted');
    expect(selectedRow!.textContent).toContain('#4');
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

  it('匿名 run（run_id=null）按 seq 回退：同一次中断不重现，新的匿名中断重现', async () => {
    await mountWith(singleInterruption(2, null));
    expect(document.querySelector('.interrupt-banner')).not.toBeNull();

    await act(async () => { buttonByText('忽略')!.click(); });
    expect(document.querySelector('.interrupt-banner')).toBeNull();

    // 同一标识（seq:2）重渲染：不重现。
    await act(async () => { H.set({ ...H.conv! }); });
    expect(document.querySelector('.interrupt-banner')).toBeNull();

    // 新的匿名中断（seq 变）：标识变化 ⇒ 重新出现。
    await act(async () => { H.set(singleInterruption(11, null)); });
    expect(document.querySelector('.interrupt-banner')).not.toBeNull();
  }, 30_000);

  it('空串 run_id 回退到 seq：两次空串中断不会共用一个标识', async () => {
    await mountWith(singleInterruption(2, ''));
    expect(document.querySelector('.interrupt-banner')).not.toBeNull();

    await act(async () => { buttonByText('忽略')!.click(); });
    expect(document.querySelector('.interrupt-banner')).toBeNull();

    // 修复前标识是 ''（`??` 不跳过空串），第二次空串中断会被静默吞掉。
    await act(async () => { H.set(singleInterruption(11, '')); });
    expect(document.querySelector('.interrupt-banner')).not.toBeNull();
  }, 30_000);

  it('跨会话同 seq 的匿名中断不互相吞：标识带会话前缀', async () => {
    await mountWith(singleInterruption(7, null, 's1'));
    await act(async () => { buttonByText('忽略')!.click(); });
    expect(document.querySelector('.interrupt-banner')).toBeNull();

    // 每会话 seq 独立：s2 的匿名中断同样 seq=7，但用户从未忽略过它。
    await act(async () => { H.set(singleInterruption(7, null, 's2')); });
    expect(document.querySelector('.interrupt-banner')).not.toBeNull();
  }, 30_000);

  it('更晚的畸形 run/interrupted（quarantine，seq=null）不抢「看缺口」落点', async () => {
    const valid = singleInterruption(2, 'run-valid');
    const withBad = applyEvent(valid, {
      type: EventType.RUN_INTERRUPTED,
      data: 'BAD-RAW',
    } as unknown as AgentEvent);
    const el = await mountWith(withBad);

    await act(async () => { buttonByText('在右栏看缺口')!.click(); });
    const peek = el.querySelector('.detail-peek');
    expect(peek).not.toBeNull();
    // 落点必须是那条合法中断（带 run_id），不是 seq=null 的 quarantine 帧。
    expect(peek!.textContent).toContain('run-valid');
    expect(peek!.textContent).toContain('run/interrupted');
  }, 30_000);

  it('事件缺失的降级：看缺口回到 run 级 focus，不保留无关旧 focus', async () => {
    const base = twoInterruptions();
    // 投影仍非 null（run_interrupted 来自第二次中断），但事件日志里没有它：
    // 模拟 P4-1 的防御性边界。
    const conv = { ...base, events: base.events.filter((e) => e.type !== EventType.RUN_INTERRUPTED) };
    const el = await mountWith(conv);

    // 先点时间线一行，制造一个无关的旧 focus（peek 打开）。
    const row = el.querySelector<HTMLElement>('.timeline-row');
    expect(row).not.toBeNull();
    await act(async () => { row!.click(); });
    expect(el.querySelector('.detail-peek')).not.toBeNull();

    await act(async () => { buttonByText('在右栏看缺口')!.click(); });
    // focusRun 收起 peek；修复前只打开 Inspector，旧 focus 原样保留。
    expect(el.querySelector('.detail-peek')).toBeNull();
  }, 30_000);

  it('「忽略」后焦点落到主工作区容器，不留在 body', async () => {
    await mountWith(singleInterruption(2, 'run-focus'));
    await act(async () => { buttonByText('忽略')!.click(); });
    expect(document.querySelector('.interrupt-banner')).toBeNull();
    expect(document.activeElement).toBe(document.getElementById('region-main'));
  }, 30_000);
});
