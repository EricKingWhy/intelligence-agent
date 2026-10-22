// @vitest-environment jsdom
/** #1444 App 层钉：**已选会话**的预算选择必须随下一次 `/messages` 提交作为 amend 发出。
 *
 *  为什么需要这一层（单测分工）：
 *   - Composer.test.tsx 锁「两态都渲染入口」（SSR）；
 *   - api.test.ts 锁 `budget` 的「有值才带键」逐字透传；
 *   - **App → useSession 这一段接线**（`handleSubmit` 的 amend 构造）此前没有任何测试，
 *     而这正是 #1444 的病灶：入口渲染了、草稿也在，但 amend 里没有 budget。
 *
 *  mock 写法照抄 App.retrySubmit.test.tsx：只 mock 数据层（`useSession` /
 *  `useProjects`）与 `BudgetPicker`（Radix Portal 壳在 jsdom 里驱动不稳，这里只借它把草稿写进 App；
 *  真面板的交互与 wire 由 e2e/budget-entry.spec.ts 锁）。真 App + 真 Conversation +
 *  真 Composer 照常渲染——Composer 的渲染门控也因此仍走真实代码。 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const NOOP = () => {};

const H = vi.hoisted(() => ({
  /** 每次 `sendMessage` 的 (sessionId, content, opts) 按调用顺序落在这里。 */
  calls: [] as { sessionId: string; content: string; opts: { amend?: Record<string, unknown> } | undefined }[],
}));

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
  const { initConversation } = await import('./lib/projection');
  return {
    ...actual,
    useSession: () => ({
      sessions: [],
      sessionsLoading: false,
      // 已选会话态：`permissionInSession={selectedId !== null}` 为真。
      selectedId: 's-1',
      conversation: initConversation('s-1'),
      historySkeleton: false,
      streaming: false,
      reconnecting: false,
      error: null,
      sessionsError: null,
      titlesById: {},
      sendFailure: null,
      runFailedBeforePlan: null,
      failedDraft: null,
      recoverState: {
        status: 'idle', message: null, conflict: false, repaired: 0,
        terminalRepaired: false, stillUnterminated: false, stillDangling: false,
        pendingDecisions: null,
      },
      selectSession: NOOP,
      submitTask: NOOP,
      sendMessage: async (
        sessionId: string,
        content: string,
        opts?: { amend?: Record<string, unknown> },
      ) => {
        H.calls.push({ sessionId, content, opts });
        return true;
      },
      cancelStream: NOOP,
      removeSession: NOOP,
      setArchived: NOOP,
      recover: NOOP,
      resumePausedRun: NOOP,
      refreshSessions: NOOP,
      changeModel: NOOP,
      changePermission: NOOP,
      exitPlanMode: NOOP,
      reloadConversation: NOOP,
      fork: NOOP,
      sendSteer: NOOP,
      flushQueue: NOOP,
      cancelItem: NOOP,
      resyncAfterDecision: NOOP,
    }),
  };
});

vi.mock('./hooks/useProjects', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useProjects')>();
  return {
    ...actual,
    useProjects: () => ({
      projects: [], loadError: null, refresh: NOOP,
      actions: new Proxy({}, { get: () => NOOP }) as never,
    }),
  };
});

/** 只保留「把草稿写进 App」这一个动作：点一下 = turns 草稿变 '5'。 */
vi.mock('./components/BudgetPicker', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./components/BudgetPicker')>();
  return {
    ...actual,
    BudgetPicker: ({ onTurnsChange }: { onTurnsChange?: (v: string) => void }) => (
      <button
        type="button"
        className="composer-budget-trigger"
        aria-label="预算"
        onClick={() => onTurnsChange?.('5')}
      >
        预算：默认
      </button>
    ),
  };
});

import App from './App';

let container: HTMLDivElement;
let root: Root;

const composer = (): HTMLTextAreaElement => container.querySelector('#composer-input')!;

async function mount(): Promise<void> {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(<App />);
  });
}

async function typeText(text: string): Promise<void> {
  await act(async () => {
    const ta = composer();
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      'value',
    )?.set;
    setter?.call(ta, text);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function pressEnter(): Promise<void> {
  await act(async () => {
    composer().dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: NOOP, removeEventListener: NOOP,
    addListener: NOOP, removeListener: NOOP, dispatchEvent: () => false,
  }));
  vi.stubGlobal('ResizeObserver', class { observe = NOOP; unobserve = NOOP; disconnect = NOOP; });
  Element.prototype.scrollIntoView = NOOP;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes('/api/capabilities') ? [] : {};
    return new Response(JSON.stringify(body), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  }));
  H.calls.length = 0;
});

afterEach(async () => {
  if (root) await act(async () => { root.unmount(); });
  container?.remove();
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

describe('#1444 已选会话的预算入口 → /messages amend', () => {
  it('入口在场；选档后提交续聊，amend 带 budget.run.max_agent_turns_total', async () => {
    await mount();

    const trigger = container.querySelector<HTMLButtonElement>('button[aria-label="预算"]');
    expect(trigger).not.toBeNull(); // 已选态也必须渲染（旧实现被 !permissionInSession 门控）

    await act(async () => {
      trigger!.click();
      await Promise.resolve();
    });

    await typeText('接着做');
    await pressEnter();

    expect(H.calls).toHaveLength(1);
    const call = H.calls[0]!;
    expect(call.sessionId).toBe('s-1');
    expect(call.content).toBe('接着做');
    expect(call.opts?.amend).toMatchObject({
      budget: { run: { max_agent_turns_total: 5 } },
    });
  });

  it('草稿全空 → amend 不携带 budget（api 层「有值才带键」，载荷逐字不变）', async () => {
    await mount();

    await typeText('普通续聊');
    await pressEnter();

    expect(H.calls).toHaveLength(1);
    const amend = H.calls[0]!.opts?.amend ?? {};
    expect(amend.budget).toBeUndefined();
    // amend 层口径：undefined 键序列化后不会出现；wire 层逐字由 api.test.ts 锁。
    expect(JSON.stringify(amend)).not.toContain('"budget"');
  });
});
