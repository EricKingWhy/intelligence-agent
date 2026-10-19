// @vitest-environment jsdom
/** App 层路由钉（#968 F2）：暂停面板的**分支选择**必须留在 App 里。
 *
 * 背景：`App.tsx` 里 `canResumePaused && paused && paused.reason !== 'user_input'`
 * 之后的第二个三元决定暂停面板走哪一支：
 *   - `stuck`            → `StuckPausedPanel`（引导 steer / 变更依据，`11 §6.1`
 *     MUST NOT 一键重试）；
 *   - `budget_exhausted` / `deadline` → `PausedPanel`（budget-increase 恢复控件）。
 * 组件测试（StuckPausedPanel.test.tsx / PausedPanel 自身用例）只钉各面板**自己的**
 * 渲染——若有人把分支改回全走 `PausedPanel`，组件测试不会红。本文件在 App 层钉住
 * 路由本身：真 App + 真面板，按 `reason` 断言挂的是哪一支。
 *
 * mock 写法照抄 `App.test.tsx`：只 mock `./hooks/useSession` 与 `./hooks/useProjects`
 * （数据层换可控夹具），App 与两个面板组件走真代码。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initConversation } from './lib/projection';
import type { ConversationState, RunPausedInfo } from './types';

// ── 可控数据层：H.conv / H.set 换引用机制照抄 App.test.tsx ──
const H = vi.hoisted(() => ({
  conv: null as ConversationState | null,
  set: null as unknown as (c: ConversationState) => void,
  submitTask: vi.fn(),
}));

const NOOP = () => {};

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
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
        submitTask: H.submitTask,
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

// ── 夹具 ─────────────────────────────────────────────────────────────────
function pausedInfo(reason: string): RunPausedInfo {
  const isStuck = reason === 'stuck';
  return {
    run_id: 'run-x',
    version: 5,
    pause_seq: 12,
    reason,
    trigger_dimension: isStuck ? 'stuck.same_name_same_args' : 'run.max_agent_turns_total',
    consumed_agent_turns: 7,
    run_limit: 20,
    consumed_dimensions: null,
    run_limits: null,
    local_fuse: { max_agent_turns: 500, source: 'deployment' },
    continuation: null,
    closeout_source: 'deterministic',
    resume_requirements: isStuck ? ['需要相关 steer，或已记录的环境 / policy 变更'] : [],
    trace_id: null,
  };
}

function conversationWithPause(reason: string): ConversationState {
  return { ...initConversation('s1'), run_paused: pausedInfo(reason) };
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function mountWith(reason: string): Promise<HTMLDivElement> {
  H.conv = conversationWithPause(reason);
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
  // createRoot 的测试必须显式声明 act 环境，否则 act() 退化成"只警告不刷新"。
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  // jsdom 不实现这三个（theme.ts 读 matchMedia；Radix/虚拟列表要 ResizeObserver；
  // 贴底滚动要 scrollIntoView）。缺哪个就在挂载时报错，故一次补齐。
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: NOOP, removeEventListener: NOOP,
    addListener: NOOP, removeListener: NOOP, dispatchEvent: () => false,
  }));
  vi.stubGlobal('ResizeObserver', class {
    observe = NOOP; unobserve = NOOP; disconnect = NOOP;
  });
  Element.prototype.scrollIntoView = NOOP;
  H.submitTask.mockClear();
  // App 挂载会拉 capabilities / models 等接口，jsdom 下给个空 200 兜底。
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

// ── 路由钉：reason 决定挂哪支面板 ─────────────────────────────────────────
describe('#968 F2 — App 层暂停面板路由（stuck 走引导面板，不走恢复控件）', () => {
  it('reason=stuck ⇒ StuckPausedPanel（引导文案），无恢复控件', async () => {
    const el = await mountWith('stuck');
    const text = el.textContent ?? '';
    expect(text).toContain('不提供一键继续');
    expect(text).not.toContain('继续这次任务');
    expect(el.querySelector('#pause-resume-ceiling')).toBeNull();
  });

  it('reason=budget_exhausted ⇒ PausedPanel（恢复控件在）', async () => {
    const el = await mountWith('budget_exhausted');
    const text = el.textContent ?? '';
    expect(text).toContain('继续这次任务');
    expect(el.querySelector('#pause-resume-ceiling')).not.toBeNull();
    expect(text).not.toContain('不提供一键继续');
  });

  it('reason=deadline ⇒ PausedPanel（恢复控件在）', async () => {
    const el = await mountWith('deadline');
    const text = el.textContent ?? '';
    expect(text).toContain('继续这次任务');
    expect(el.querySelector('#pause-resume-ceiling')).not.toBeNull();
    expect(text).not.toContain('不提供一键继续');
  });
});
