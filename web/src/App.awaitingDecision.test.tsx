// @vitest-environment jsdom
/** #1030 呈现层（缺陷②/③，AC2/AC3）：待裁决横幅与人话文案。
 *
 *  渲染**真 App**，只把数据 hook（`useSession`）与 `useProjects` 换成可控夹具
 *  ——横幅的可见性与文案全是 App 的派生逻辑，抄进测试宿主就只测到副本。
 *
 *  锁三件事：
 *   1. AC3：**无需任何 409**，只要后端清单非空，横幅与「去裁决」入口就在场；
 *   2. AC2：横幅第一层是人话，**不含**任一协议词汇；后端原文只在 `<details>` 里；
 *   3. AC2/AC3：`null`（未知）与 `[]`（后端说没有）都**不**出横幅——没有事实就不报态。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PendingDecision } from './lib/api';

const H = vi.hoisted(() => ({
  recoverState: null as unknown,
  submitDecisions: null as unknown,
}));
const NOOP = () => {};

/** 后端真原文（`session/service.py:5061-5065`）——它出现在 recover 409 的 message 里。 */
const PROTOCOL_MESSAGE =
  '存在需要人工裁决的 UNKNOWN Operation（bash(tool_call_id=call_82ade…)）：先 POST ' +
  '/api/sessions/{s}/recover 携带 decisions=[{tool_call_id, verdict}] 结清（verdict ∈ CONFIRM_SUCCESS / …）';

const PENDING: PendingDecision[] = [
  {
    tool_call_id: 'call_82ade',
    tool_name: 'bash',
    state: 'UNKNOWN',
    default_action: 'RETRY',
    risk_level: 'high',
    probe: { verifiable: false, suggested_action: null },
  },
];

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
  return {
    ...actual,
    useSession: () => ({
      sessions: [],
      selectedId: 's-1',
      conversation: null,
      loadingHistory: false,
      streaming: false,
      reconnecting: false,
      error: null,
      sessionsError: null,
      titlesById: {},
      recoverState: H.recoverState,
      selectSession: NOOP,
      submitTask: NOOP,
      sendMessage: NOOP,
      cancelStream: NOOP,
      removeSession: NOOP,
      setArchived: NOOP,
      recover: NOOP,
      submitDecisions: H.submitDecisions,
      refreshSessions: NOOP,
      changeModel: NOOP,
      fork: NOOP,
      sendSteer: NOOP,
      flushQueue: NOOP,
      cancelItem: NOOP,
    }),
  };
});

vi.mock('./hooks/useProjects', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useProjects')>();
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

let container: HTMLDivElement | null = null;
let root: Root | null = null;

/** 由 `pendingDecisions` 造出一份 recover 态（其余字段保持 idle）。 */
function recoverStateWith(pendingDecisions: PendingDecision[] | null, message: string | null = null) {
  return {
    status: 'idle', message, conflict: false, repaired: 0,
    terminalRepaired: false, stillUnterminated: false, stillDangling: false,
    pendingDecisions,
  };
}

async function mount(): Promise<void> {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(<App />);
  });
}

function banner(): HTMLElement | null {
  return container!.querySelector<HTMLElement>('.adjudication-banner');
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  // jsdom 不实现这三个（theme.ts 读 matchMedia；Radix/虚拟列表要 ResizeObserver；
  // 贴底滚动要 scrollIntoView）——同 App.test.tsx 的既有做法。
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
      const body = url.includes('/api/capabilities') ? [] : {};
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }),
  );
  H.recoverState = recoverStateWith(null);
  H.submitDecisions = vi.fn();
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  root = null;
  container = null;
  vi.unstubAllGlobals();
});

describe('#1030 待裁决横幅（AC2/AC3）', () => {
  it('AC3：清单非空 ⇒ 裁决面板**自动**在场（零 409、零点击）', async () => {
    // 这正是缺陷②的修法：此前面板只在"点恢复会话 → 撞一次带 pending_decisions 的
    // 409"之后才出现；现在后端清单非空就直接驱动它。
    H.recoverState = recoverStateWith(PENDING, PROTOCOL_MESSAGE);
    await mount();
    expect(container!.querySelector('.recovery-decision-panel')).not.toBeNull();
    expect(container!.textContent).toContain('结果不确定');
  });

  it('AC3：关掉面板后横幅接替成为常驻入口（不是"关了就再也找不到"）', async () => {
    H.recoverState = recoverStateWith(PENDING, PROTOCOL_MESSAGE);
    await mount();
    const close = container!.querySelector('.recovery-decision-close') as HTMLButtonElement;
    await act(async () => {
      close.click();
    });
    expect(container!.querySelector('.recovery-decision-panel')).toBeNull();

    const el = banner();
    expect(el).not.toBeNull();
    const openBtn = el!.querySelector('.adjudication-open-btn') as HTMLButtonElement;
    expect(openBtn.textContent).toContain('去裁决');

    // 点它 → 面板回来（横幅是**动作**，不是只读装饰）。
    await act(async () => {
      openBtn.click();
    });
    expect(container!.querySelector('.recovery-decision-panel')).not.toBeNull();
  });

  it('AC2：横幅第一层是人话，且**不含**任何协议词汇', async () => {
    H.recoverState = recoverStateWith(PENDING, PROTOCOL_MESSAGE);
    await mount();
    // 先关掉面板，让横幅成为可观测的入口。
    await act(async () => {
      (container!.querySelector('.recovery-decision-close') as HTMLButtonElement).click();
    });
    const headline = container!.querySelector('.adjudication-headline')!;
    expect(headline.textContent).toContain('裁决');
    for (const token of ['POST /api/sessions', 'tool_call_id', 'verdict ∈', 'decisions=[']) {
      expect(headline.textContent, `人话层泄漏了 ${token}`).not.toContain(token);
    }
  });

  it('AC2：后端原文进折叠层——逐字保留，不是消失（排查/手工结清的唯一依据）', async () => {
    H.recoverState = recoverStateWith(PENDING, PROTOCOL_MESSAGE);
    await mount();
    // 面板里的技术详情层（面板自己是第一层人话）。
    const details = container!.querySelector('.recovery-decision-panel .adjudication-tech') as HTMLDetailsElement;
    expect(details).not.toBeNull();
    expect(details.open).toBe(false); // 默认折叠
    expect(details.querySelector('summary')!.textContent).toContain('技术详情');
    const body = details.querySelector('.adjudication-tech-body')!;
    expect(body.textContent).toBe(PROTOCOL_MESSAGE);
    expect(body.textContent).toContain('tool_call_id');

    // 且**面板的人话标题**里没有协议词汇。
    const title = container!.querySelector('.recovery-decision-title')!;
    for (const token of ['POST /api/sessions', 'tool_call_id', 'verdict ∈']) {
      expect(title.textContent).not.toContain(token);
    }
  });

  it('`[]`（后端明确说没有）⇒ 不出面板也不出横幅——没有事实就不报态', async () => {
    H.recoverState = recoverStateWith([]);
    await mount();
    expect(banner()).toBeNull();
    expect(container!.querySelector('.recovery-decision-panel')).toBeNull();
  });

  it('`null`（未知 / 读不到）⇒ 同样什么都不出，且不冒称"没事"', async () => {
    // 读不到 ≠ 没事：既不报「待裁决」（没有事实），也不报任何"已结清"的承诺。
    H.recoverState = recoverStateWith(null);
    await mount();
    expect(banner()).toBeNull();
    expect(container!.querySelector('.recovery-decision-panel')).toBeNull();
  });

  it('没有 message 时不画空的折叠层（没有可展开的内容）', async () => {
    H.recoverState = recoverStateWith(PENDING, null);
    await mount();
    expect(container!.querySelector('.recovery-decision-panel')).not.toBeNull();
    expect(container!.querySelector('.adjudication-tech')).toBeNull();
  });
});
