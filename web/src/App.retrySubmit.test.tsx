// @vitest-environment jsdom
/** #1367（P0-3 / P1-2 / P1-3）App 层钉：断网失败条里的「重试」必须
 *
 *  ① 重发**失败的那一次**（payload 逐字相同，不是某个共享槽位里的旧任务）；
 *  ② 只发一次（连点被同步闸挡住，不产生第二个 `POST /api/sessions`）；
 *  ③ 重试受理成功后消费草稿（Composer 自己的清空链不会为这条重试跑）。
 *
 * mock 写法照抄 App.askUserRouting.test.tsx：只 mock 数据层（`useSession`），
 * 真 App + 真 Conversation + 真 Composer。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const NOOP = () => {};

const H = vi.hoisted(() => ({
  sendFailure: null as unknown,
  setSendFailure: null as unknown as (f: unknown) => void,
  /** 每次 `submitTask` 的 (payload, opts) 按调用顺序落在这里。 */
  calls: [] as { payload: unknown; opts: unknown }[],
  failNext: true,
  /** 让 mock 回执声明 `retryable:true` 却不携带动作（hook 契约允许的形状）。 */
  dropRetry: false,
}));

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
  const React = await import('react');
  const { initConversation } = await import('./lib/projection');
  return {
    ...actual,
    useSession: () => {
      const [sendFailure, setSendFailure] = React.useState<unknown>(H.sendFailure);
      H.setSendFailure = setSendFailure;
      return {
        sessions: [],
        selectedId: null,
        conversation: initConversation('s-new'),
        loadingHistory: false,
        streaming: false,
        reconnecting: false,
        error: null,
        sessionsError: null,
        titlesById: {},
        sendFailure,
        runFailedBeforePlan: null,
        recoverState: {
          status: 'idle', message: null, conflict: false, repaired: 0,
          terminalRepaired: false, stillUnterminated: false, stillDangling: false,
          pendingDecisions: null,
        },
        selectSession: NOOP,
        submitTask: async (payload: unknown, opts: { onRetry?: unknown }) => {
          H.calls.push({ payload, opts });
          if (H.failNext) {
            H.failNext = false;
            const failure = {
              message: '提交失败：Failed to fetch',
              retryable: true,
              ...(H.dropRetry ? {} : { onRetry: opts?.onRetry }),
            };
            H.setSendFailure(failure);
            return failure.message;
          }
          H.setSendFailure(null);
          return null;
        },
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
        changePermission: NOOP,
        resumePausedRun: NOOP,
      };
    },
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
    // 两拍：Composer 的 Promise.resolve 链 + App 的 async 提交体各一拍。
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
  H.sendFailure = null;
  H.calls.length = 0;
  H.failNext = true;
  H.dropRetry = false;
});

afterEach(async () => {
  if (root) await act(async () => { root.unmount(); });
  container?.remove();
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

describe('#1367 P0-3 — 断网失败条内的重试', () => {
  it('重发失败那一次（payload 逐字相同）· 连点只发一次 · 成功后消费草稿', async () => {
    await mount();
    await typeText('写一个 FizzBuzz 脚本并运行验证');
    await pressEnter();

    expect(H.calls).toHaveLength(1);
    const firstPayload = H.calls[0]!.payload;

    const bar = container.querySelector('.conversation-error');
    expect(bar).not.toBeNull();
    const retry = bar!.querySelector<HTMLButtonElement>('button.conversation-error-retry');
    expect(retry).not.toBeNull();
    expect(retry!.disabled).toBe(false);

    // 连点：第二下必须被同步闸挡掉（state 的 retryPending 挡不住同一 tick）。
    await act(async () => {
      retry!.click();
      retry!.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(H.calls).toHaveLength(2);
    expect(H.calls[1]!.payload).toEqual(firstPayload);
    expect(H.calls[1]!.payload).toMatchObject({ task: '写一个 FizzBuzz 脚本并运行验证' });

    // 受理成功 ⇒ 草稿被消费（这条重试不经过 Composer 自己的清空链）。
    expect(composer().value).toBe('');
    // 失败条随之消失（回执被清）。
    expect(container.querySelector('.conversation-error')).toBeNull();
  });

  /* 审查（WIP `28efd9d1`）修的正是这条：`retryable` 由写入点声明，而动作由调用方
     给；两者不一致时文案不能许诺一个不存在的按钮。 */
  it('回执 retryable 但不带动作 ⇒ 不给按钮，文案也不承诺「点重试」', async () => {
    H.dropRetry = true;
    await mount();
    await typeText('写一个 FizzBuzz 脚本并运行验证');
    await pressEnter();

    const bar = container.querySelector('.conversation-error');
    expect(bar).not.toBeNull();
    expect(bar!.querySelector('button.conversation-error-retry')).toBeNull();
    const hint = bar!.querySelector('.conversation-error-hint')?.textContent ?? '';
    expect(hint).toContain('重新发送');
    expect(hint).not.toContain('点重试');
  });
});
