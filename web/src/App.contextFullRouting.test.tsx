// @vitest-environment jsdom
/** App 层路由钉（#1197 项 2）：上下文满时的暂停**不许再弹技术屏**。
 *
 * 背景：`App.tsx` 里 `canResumePaused && paused && paused.reason !== 'user_input'`
 * 之后的第二个三元决定暂停面板走哪一支（`#968` 立的路由点）：
 *   - `stuck`            → `StuckPausedPanel`（引导 steer / 变更依据）；
 *   - **context 暂停**   → `ContextFullDialog`（一句话 + 换模型/结束）← 本项新增；
 *   - 其余（budget/deadline）→ `PausedPanel`（budget-increase 恢复控件）。
 *
 * 判别口径是 `pauseFacts().resumeTarget.kind === 'context'`（即
 * `trigger_dimension === 'max_context_tokens'`，`#1026` 裁决 A），**不是** `reason`
 * ——context 暂停的 `reason` 是 `budget_exhausted`，与普通预算暂停同一个字符串。
 *
 * 组件测试（ContextFullDialog.test.tsx / PausedPanel.test.tsx）只钉各面板**自己的**
 * 渲染；若有人把分支改回全走 `PausedPanel`，它们不会红。本文件在 App 层钉住路由本身。
 *
 * mock 写法照抄 `App.stuckPauseRouting.test.tsx`。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initConversation } from './lib/projection';
import type { ConversationState, RunPausedInfo } from './types';

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

/** context 暂停：`reason` 仍是 `budget_exhausted`（后端事实），判别靠 trigger_dimension。 */
function contextPaused(): RunPausedInfo {
  return {
    run_id: 'run-ctx',
    version: 5,
    pause_seq: 12,
    reason: 'budget_exhausted',
    trigger_dimension: 'max_context_tokens',
    consumed_agent_turns: 5,
    run_limit: 20,
    consumed_dimensions: {
      agent_turns: 5, model_requests: 9, total_tokens: 160_000, cost_usd: null,
      tool_calls: 4, tool_attempts: 4,
      tool_calls_by_tool: { read: 2 }, tool_attempts_by_tool: { read: 2 },
    },
    run_limits: {
      max_agent_turns_total: 20, max_model_requests: null, max_total_tokens: null,
      max_cost_usd: null, deadline_at: null, tool_call_limits: {},
    },
    local_fuse: { max_agent_turns: 500, source: 'deployment' },
    continuation: null,
    closeout_source: 'deterministic',
    resume_requirements: [],
    trace_id: null,
  };
}

function budgetPaused(): RunPausedInfo {
  return {
    ...contextPaused(),
    run_id: 'run-budget',
    trigger_dimension: 'run.max_agent_turns_total',
  };
}

function conversationWithPause(paused: RunPausedInfo): ConversationState {
  return { ...initConversation('s1'), run_paused: paused };
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function mountWith(paused: RunPausedInfo): Promise<HTMLDivElement> {
  H.conv = conversationWithPause(paused);
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
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
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
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.includes('/api/models')
        ? { models: [{ name: 'big-window', provider: 'p', model: 'm', default: true }] }
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

describe('#1197 项 2 — 上下文满时走一句话弹窗，不走技术屏', () => {
  it('context 暂停 ⇒ 一句话弹窗，且技术屏的每一处误导文案都不在', async () => {
    const el = await mountWith(contextPaused());
    const text = el.textContent ?? '';
    const body = document.body.textContent ?? '';

    // 该有的：票面那一句话 + 两个按钮。
    expect(body).toContain('记忆容量已经满了');
    expect(body).toContain('前面没有可以精简的内容，换一个记忆容量更大的模型继续吧。');
    expect(body).toContain('换模型');
    expect(body).toContain('结束');

    // 不该有的：技术屏本体与它的内部术语。
    expect(text).not.toContain('预算已用完，任务已暂停');
    expect(text).not.toContain('继续这次任务');
    expect(text).not.toContain('max_context_tokens');
    expect(text).not.toContain('预算版本');
    expect(el.querySelector('#pause-resume-ceiling')).toBeNull();
  });

  it('普通预算暂停（reason 同一个 budget_exhausted）仍走 PausedPanel 恢复控件', async () => {
    const el = await mountWith(budgetPaused());
    const text = el.textContent ?? '';
    const body = document.body.textContent ?? '';

    expect(text).toContain('预算已用完，任务已暂停');
    expect(text).toContain('继续这次任务');
    expect(el.querySelector('#pause-resume-ceiling')).not.toBeNull();
    // 反向：这句话弹窗不该在预算暂停时出现。
    expect(body).not.toContain('记忆容量已经满了');
  });

  it('「换模型」真的打开模型选择器（不是死按钮）', async () => {
    await mountWith(contextPaused());
    // 弹窗开着时先确认选择器没开。
    expect(document.body.querySelector('[role="menu"]')).toBeNull();

    const changeModel = Array.from(document.body.querySelectorAll('button')).find((b) =>
      (b.textContent ?? '').includes('换模型'),
    );
    expect(changeModel).toBeTruthy();
    await act(async () => {
      changeModel!.click();
    });

    // Radix 的菜单开在 pointerdown 上（Trigger 只 compose onPointerDown，没有
    // onClick），所以这一条同时钉住"App 派发的是 pointerdown 而不是 click"。
    expect(document.body.querySelector('[role="menu"]')).not.toBeNull();
  });

  it('「结束」收起这一屏（不假装能终止 run——后端没有那个动作）', async () => {
    await mountWith(contextPaused());
    expect(document.body.textContent ?? '').toContain('记忆容量已经满了');

    const endRun = Array.from(document.body.querySelectorAll('button')).find((b) =>
      (b.textContent ?? '').includes('结束'),
    );
    expect(endRun).toBeTruthy();
    await act(async () => {
      endRun!.click();
    });

    expect(document.body.textContent ?? '').not.toContain('记忆容量已经满了');
    // 收起 ≠ 会话被清空：run_paused 事实还在（面板消失只是 UI 意图）。
    expect(H.conv?.run_paused?.run_id).toBe('run-ctx');
  });
});
