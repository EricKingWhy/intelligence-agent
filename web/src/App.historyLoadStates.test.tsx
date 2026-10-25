// @vitest-environment jsdom
/** #1521 App 层接线钉（审查 B-F1）：useSession → App（useMemo 门）→ Conversation
 *  失败面板 / 温和提示横幅这条**接线缝**此前零红证——hook 层与组件层各自正确
 *  不等于接对了。锁四件事：
 *
 *  ① 门的三条件（sid 匹配 + conversation===null + historyLoadError 非空）同时成立
 *     ⇒ Conversation 渲染失败面板（role=alert + 原文 + 重试→reloadConversation）；
 *  ② sid 不匹配 ⇒ 门关死，仍是「暂无对话」空态（跨会话残留在渲染层不可达）；
 *  ③ missingSessionNotice ⇒ app-notice 横幅在位，关闭按钮走 dismiss；
 *  ④ error 横幅通道不再复述同一句首载失败（矛盾组合在 App 层也不出现）。
 *
 * mock 写法照抄 App.retrySubmit.test.tsx：只 mock 数据层（`useSession`），
 * 真 App + 真 Conversation。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const NOOP = () => {};

const H = vi.hoisted(() => ({
  historyLoadError: null as unknown,
  missingSessionNotice: null as unknown,
  dismissMissingSessionNotice: null as unknown as () => void,
  reloadCalls: [] as string[],
}));

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
  const React = await import('react');
  return {
    ...actual,
    useSession: () => {
      const [historyLoadError] = React.useState<unknown>(H.historyLoadError);
      const [missingSessionNotice] = React.useState<unknown>(H.missingSessionNotice);
      return {
        sessions: [],
        selectedId: 's-1521',
        conversation: null,
        loadingHistory: false,
        historySkeleton: false,
        streaming: false,
        reconnecting: false,
        error: null,
        sessionsError: null,
        titlesById: {},
        sendFailure: null,
        historyLoadError,
        missingSessionNotice,
        dismissMissingSessionNotice: () => { H.dismissMissingSessionNotice(); },
        runFailedBeforePlan: null,
        recoverState: {
          status: 'idle', message: null, conflict: false, repaired: 0,
          terminalRepaired: false, stillUnterminated: false, stillDangling: false,
          pendingDecisions: null,
        },
        failedDraft: null,
        selectSession: NOOP,
        reloadConversation: (sid: string) => { H.reloadCalls.push(sid); },
        submitTask: async () => null,
        sendMessage: NOOP,
        cancelStream: NOOP,
        removeSession: NOOP,
        setArchived: NOOP,
        recover: NOOP,
        submitDecisions: NOOP,
        refreshSessions: NOOP,
        refreshPendingDecisions: NOOP,
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

async function mount(): Promise<void> {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(<App />);
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
  H.historyLoadError = null;
  H.missingSessionNotice = null;
  H.dismissMissingSessionNotice = NOOP;
  H.reloadCalls = [];
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe('#1521 App 接线：历史加载失败面板', () => {
  it('门三条件成立 ⇒ 失败面板（role=alert + 原文 + 重试→reloadConversation），横幅与「暂无对话」都不出现', async () => {
    H.historyLoadError = { sid: 's-1521', message: '模拟服务端 500' };
    await mount();

    const panel = container.querySelector('.empty-state');
    expect(panel).not.toBeNull();
    expect(panel!.getAttribute('role')).toBe('alert');
    expect(container.querySelector('.empty-hero')?.textContent).toBe('历史加载失败');
    expect(container.querySelector('.empty-sub')?.textContent).toBe('模拟服务端 500');
    expect(container.textContent).not.toContain('暂无对话');
    // 矛盾组合的消除在 App 层同样成立：error 槽（mock 恒 null）不渲染 app-error
    expect(container.querySelector('.app-error')).toBeNull();

    const retry = container.querySelector<HTMLElement>('.empty-examples .example-chip');
    expect(retry?.textContent).toBe('重试');
    await act(async () => { retry!.click(); });
    expect(H.reloadCalls).toEqual(['s-1521']);
  });

  it('historyLoadError 属于别的会话 ⇒ 门关死，仍是「暂无对话」空态', async () => {
    H.historyLoadError = { sid: 's-旧', message: '陈旧失败' };
    await mount();

    expect(container.querySelector('.empty-hero')?.textContent).toBe('暂无对话');
    expect(container.textContent).not.toContain('历史加载失败');
    expect(container.textContent).not.toContain('陈旧失败');
    // 空态示例 chip 照旧（那是 onPresetTask 的入口）；没有一个是「重试」chip
    const chips = [...container.querySelectorAll<HTMLElement>('.empty-examples .example-chip')];
    expect(chips.some((c) => c.textContent === '重试')).toBe(false);
  });
});

describe('#1521 App 接线：显式 404 温和提示', () => {
  it('missingSessionNotice ⇒ app-notice 在位；关闭按钮走 dismissMissingSessionNotice', async () => {
    H.missingSessionNotice = '这个会话已不存在——可能已被删除。列表正在刷新。';
    const dismiss = vi.fn();
    H.dismissMissingSessionNotice = dismiss;
    await mount();

    const notice = container.querySelector('.app-notice');
    expect(notice).not.toBeNull();
    expect(notice!.textContent).toContain('这个会话已不存在');

    const close = container.querySelector<HTMLElement>('.app-notice .auth-banner-close');
    expect(close?.getAttribute('aria-label')).toBe('关闭提示');
    await act(async () => { close!.click(); });
    expect(dismiss).toHaveBeenCalledTimes(1);
  });
});
