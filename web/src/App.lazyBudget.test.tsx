// @vitest-environment jsdom
/** #1467 App 层钉：预算入口与**两条**提交路径的正确分工。
 *
 *  病灶：`ensureSession` 的懒建请求（`POST /api/sessions?launch=false`）曾携带
 *  `budget.run`，而后端对该组合恒 422（`budget.run` 只属于启动 run 的请求）。
 *  修法：懒建只建身份（+ 权限档 + cwd），预算随**首条消息**的 `/messages` amend
 *  发出（与 #1444 的已选态同一映射点 `toCreateBudget`）。
 *
 *  为什么钉在 App 层：`toCreateBudget` 的判形与 api 层的「有值才带键」各有单测
 *  （amend.test.ts / api.test.ts），缺的正是 **App → createEmptySession / sendMessage
 *  这段接线**——病灶就在这里。
 *
 *  mock 写法照抄 App.retrySubmit.test.tsx：只 mock 数据层（`useSession` /
 *  `useProjects` / `createEmptySession`）与 `BudgetPicker`（Radix Portal 壳在 jsdom
 * 里驱动不稳，这里只借它把草稿写进 App；真面板交互由 e2e/budget-entry.spec.ts 锁）。
 *  真 App + 真 Conversation + 真 Composer 照常渲染。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const NOOP = () => {};

const H = vi.hoisted(() => ({
  /** 每次 `createEmptySession` 的载荷（懒建请求体形状的来源）。 */
  createCalls: [] as Record<string, unknown>[],
  /** 每次 `sendMessage` 的 (sessionId, content, opts)。 */
  sendCalls: [] as {
    sessionId: string;
    content: string;
    opts: { amend?: Record<string, unknown> } | undefined;
  }[],
}));

vi.mock('./lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./lib/api')>();
  return {
    ...actual,
    createEmptySession: async (payload: Record<string, unknown>) => {
      H.createCalls.push(payload);
      return { sessionId: 's-new' };
    },
  };
});

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
  const { initConversation } = await import('./lib/projection');
  return {
    ...actual,
    useSession: () => ({
      sessions: [],
      // 新建态：`permissionInSession=false` ⇒ 预算入口在 Composer 里可见。
      selectedId: null,
      conversation: initConversation('s-new'),
      loadingHistory: false,
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
      sendMessage: async (
        sessionId: string,
        content: string,
        opts?: { amend?: Record<string, unknown> },
      ) => {
        H.sendCalls.push({ sessionId, content, opts });
        return true;
      },
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
      changePermission: NOOP,
      resumePausedRun: NOOP,
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

/** 打开附件「+」菜单并勾选「作为可复用规则」（#1405：规则开关已收进该菜单）。 */
async function enableRule(): Promise<void> {
  await act(async () => {
    const trigger = container.querySelector<HTMLButtonElement>('button[aria-label="添加图片"]')!;
    trigger.focus();
    trigger.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
    );
  });
  const ruleItem = document.querySelector<HTMLElement>('#remember-as-procedural-rule');
  expect(ruleItem).not.toBeNull();
  await act(async () => ruleItem!.click());
}

/** 选「本项目」→ 非会话级登记 ⇒ `handleSubmit` 先懒建再走 `/messages`（首条消息）。 */
async function chooseProjectScope(): Promise<void> {
  const pills = [...container.querySelectorAll<HTMLButtonElement>('button.composer-scope-pill')];
  const project = pills.find((b) => (b.textContent ?? '').trim() === '本项目');
  expect(project).toBeDefined();
  await act(async () => project!.click());
}

async function pressEnter(): Promise<void> {
  await act(async () => {
    composer().dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
    );
    // Composer 的 Promise.resolve 链 + App 的 async 提交体（含懒建）各若干拍。
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
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
  H.createCalls.length = 0;
  H.sendCalls.length = 0;
});

afterEach(async () => {
  if (root) await act(async () => { root.unmount(); });
  container?.remove();
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

/** 选预算 → 勾规则 → 选「本项目」→ 回车（非会话级登记 ⇒ 懒建 + 首条消息）。 */
async function submitWithBudget(): Promise<void> {
  await mount();
  const trigger = container.querySelector<HTMLButtonElement>('button[aria-label="预算"]');
  expect(trigger).not.toBeNull(); // 新建态预算入口在（permissionInSession=false）
  await act(async () => {
    trigger!.click();
    await Promise.resolve();
  });
  await enableRule();
  await chooseProjectScope();
  await typeText('本项目都要 TDD');
  await pressEnter();
}

describe('#1467 懒建不带 budget、首条消息 amend 带 budget', () => {
  it('懒建载荷无 budget 键（budget.run 与 launch=false 后端 422 互斥）', async () => {
    await submitWithBudget();
    expect(H.createCalls).toHaveLength(1);
    expect(H.createCalls[0]).not.toHaveProperty('budget');
  }, 15_000);

  it('首条消息 amend 带 budget.run.max_agent_turns_total', async () => {
    await submitWithBudget();
    expect(H.sendCalls).toHaveLength(1);
    expect(H.sendCalls[0]!.sessionId).toBe('s-new');
    expect(H.sendCalls[0]!.opts?.amend).toMatchObject({
      budget: { run: { max_agent_turns_total: 5 } },
    });
  }, 15_000);

  it('草稿全空 → 懒建与 amend 都不含 budget（载荷逐字不变）', async () => {
    await mount();
    await enableRule();
    await chooseProjectScope();
    await typeText('普通首条消息');
    await pressEnter();

    expect(H.createCalls).toHaveLength(1);
    expect(H.createCalls[0]).not.toHaveProperty('budget');
    expect(H.sendCalls).toHaveLength(1);
    const amend = H.sendCalls[0]!.opts?.amend ?? {};
    expect(amend.budget).toBeUndefined();
    expect(JSON.stringify(amend)).not.toContain('"budget"');
  }, 15_000);
});
