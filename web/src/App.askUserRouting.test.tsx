// @vitest-environment jsdom
/** #1204 App 层路由钉：`USER_INPUT_REQUESTED` 的两支**必须按 kind 分流挂载**——
 *  `kind=ask_user` → 对话流内嵌 `AskUserCard`；`kind=protected_fact_conflict`（及其它）
 *  → 原模态 `ConstraintResolutionDialog`。组件测试只钉各自渲染，管不到"App 挂哪一支"；
 *  本文件用真 App + 真 Conversation + 真投影，按事件 kind 断言挂的是哪一支、且互斥。
 *
 *  mock 写法照抄 App.test.tsx / App.stuckPauseRouting.test.tsx：只 mock 数据层。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { projectHistory } from './lib/projection';
import type { AgentEvent, ConversationState } from './types';

const H = vi.hoisted(() => ({
  conv: null as ConversationState | null,
  set: null as unknown as (c: ConversationState) => void,
}));

const NOOP = () => {};

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
  const React = await import('react');
  return {
    ...actual,
    useSession: () => {
      const [conv] = React.useState(H.conv);
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

const SID = 's1';
const T = '2026-10-16T00:00:00.000Z';

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  return { data: {}, seq: null, run_id: null, step_id: null, session_id: SID, time: T, ...partial } as AgentEvent;
}

interface MountOptions {
  /** 随请求落盘的超时快照：双写 `timeout_seconds`/`timeout_minutes`（#1224/#1284）。 */
  seconds?: number;
  minutes?: number;
  /** 答复形态：`answered` = 选了选项；`skipped` = 整卡跳过（每题 `answer=null`）。 */
  answer?: 'answered' | 'skipped';
  /** 是否带 `run/paused`（卡片挂载的必要条件之一）。 */
  paused?: boolean;
}

/** 造一段"有一轮用户消息 + 一个待回答请求"的会话事件——Conversation 只有在
 *  `turns.length > 0` 时才走对话流渲染区（空态不渲染内嵌卡片）。 */
function events(
  kind: 'ask_user' | 'protected_fact_conflict', opts: MountOptions = {},
): AgentEvent[] {
  const { seconds, minutes, answer, paused = true } = opts;
  const requestData = kind === 'ask_user'
    ? {
        request_id: `${kind}-1`, kind, run_id: 'r1',
        ...(seconds === undefined ? {} : { timeout_seconds: seconds }),
        ...(minutes === undefined ? {} : { timeout_minutes: minutes }),
        questions: [
          {
            question: '新版本什么时候发布？', header: '发布决策', recommended: 0,
            options: [{ label: '今晚低峰期' }, { label: '明早 9 点' }],
          },
        ],
      }
    : {
        request_id: `${kind}-1`, kind, run_id: 'r1',
        fact_id: 'fact-1', old_value: 'Use Python', candidate: 'Use TypeScript',
        question: 'Choose how to resolve this conflict.',
        source_event_id: 'source-1', source_event_seq: 0,
        choices: [
          { id: 'replace_persistently', label: 'Replace persistently' },
          { id: 'current_task_only', label: 'This task only' },
          { id: 'keep_existing', label: 'Keep existing' },
          { id: 'custom', label: 'Custom reply' },
        ],
      };
  const tail: AgentEvent[] = paused
    ? [
        ev({
          type: 'run/paused', seq: 5, run_id: 'r1',
          data: { reason: 'user_input', input_request_id: `${kind}-1` },
        }),
      ]
    : [];
  // 答复事件（#1283 P2-3）：`pending_ask_user.answer` 由 `user/message` 的
  // `input_request_answer` 回填；整卡跳过是「每题 answer=null」的非 null 答案对象。
  const answered: AgentEvent[] = answer === undefined
    ? []
    : [
        ev({
          type: 'user/message', seq: 6, run_id: 'r1', step_id: 1,
          data: {
            content: '',
            input_request_id: `${kind}-1`,
            input_request_answer: kind === 'ask_user'
              ? {
                  request_id: `${kind}-1`,
                  questions: [{
                    question: '新版本什么时候发布？',
                    answer: answer === 'skipped' ? null : { label: '今晚低峰期' },
                  }],
                }
              : {
                  request_id: `${kind}-1`,
                  choice: 'keep_existing',
                },
          },
        }),
      ];
  return [
    ev({ type: 'session/started', seq: 1, run_id: 'r1' }),
    ev({ type: 'run/started', seq: 2, run_id: 'r1' }),
    ev({ type: 'user/message', seq: 3, run_id: 'r1', step_id: 1, data: { content: '走起', step: 1 } }),
    ev({ type: 'user/input-requested', seq: 4, run_id: 'r1', data: requestData }),
    ...tail,
    ...answered,
  ];
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

async function mount(
  kind: 'ask_user' | 'protected_fact_conflict', opts: MountOptions = {},
): Promise<HTMLDivElement> {
  H.conv = projectHistory(SID, events(kind, opts));
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
  vi.stubGlobal('ResizeObserver', class { observe = NOOP; unobserve = NOOP; disconnect = NOOP; });
  Element.prototype.scrollIntoView = NOOP;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes('/api/capabilities') ? [] : {};
    return new Response(JSON.stringify(body), {
      status: 200, headers: { 'content-type': 'application/json' },
    });
  }));
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

describe('#1204 — App 按 kind 分流挂载提问 UI', () => {
  it('kind=ask_user ⇒ 对话流内嵌 AskUserCard，不挂模态约束弹窗', async () => {
    const el = await mount('ask_user');
    expect(el.querySelector('.ask-user-card')).not.toBeNull();
    expect(document.querySelector('.constraint-input-dialog')).toBeNull();
  });

  it('kind=protected_fact_conflict ⇒ 原模态约束弹窗，不挂 AskUserCard', async () => {
    await mount('protected_fact_conflict');
    expect(document.querySelector('.constraint-input-dialog')).not.toBeNull();
    expect(document.querySelector('.ask-user-card')).toBeNull();
  });
});

describe('#1223 — 待回答的用户输入锁 composer', () => {
  it('kind=ask_user 待回答 ⇒ composer 锁定，提示中性（把用户引向卡片，不撞 409）', async () => {
    const el = await mount('ask_user');
    const textarea = el.querySelector('textarea.composer');
    expect(textarea).not.toBeNull();
    expect(textarea!.hasAttribute('disabled')).toBe(true);
    const hint = el.querySelector('.composer-locked-hint');
    expect(hint?.textContent).toContain('请先回答当前提问，再发送新消息');
    expect(hint?.textContent).not.toContain('约束澄清');
  });

  it('kind=protected_fact_conflict 待回答 ⇒ composer 锁定，约束澄清文案保持不动', async () => {
    const el = await mount('protected_fact_conflict');
    const textarea = el.querySelector('textarea.composer');
    expect(textarea!.hasAttribute('disabled')).toBe(true);
    const hint = el.querySelector('.composer-locked-hint');
    expect(hint?.textContent).toContain('请先回答当前约束澄清，再发送新消息');
  });

  it('#1283 P2-1：有请求、无 run/paused ⇒ 不锁（没有卡片可答时绝不锁死输入框）', async () => {
    const el = await mount('ask_user', { paused: false });
    // 卡片挂载要求「请求与暂停对齐」；这里没有暂停 ⇒ 没有卡可答。
    expect(el.querySelector('.ask-user-card')).toBeNull();
    const textarea = el.querySelector('textarea.composer');
    expect(textarea).not.toBeNull();
    expect(textarea!.hasAttribute('disabled')).toBe(false);
    expect(el.querySelector('.composer-locked-hint')).toBeNull();
  });

  it('#1283 复审：约束澄清同样「有请求、无 run/paused ⇒ 不锁」（与卡片同源）', async () => {
    const el = await mount('protected_fact_conflict', { paused: false });
    // 约束弹窗挂载同样要求「请求与暂停对齐」；这里没有暂停 ⇒ 没有可答的卡。
    expect(document.querySelector('.constraint-input-dialog')).toBeNull();
    const textarea = el.querySelector('textarea.composer');
    expect(textarea).not.toBeNull();
    expect(textarea!.hasAttribute('disabled')).toBe(false);
    expect(el.querySelector('.composer-locked-hint')).toBeNull();
  });

  it('#1283 P2-3：已答复（answer !== null）⇒ 锁解除', async () => {
    const el = await mount('ask_user', { answer: 'answered' });
    const textarea = el.querySelector('textarea.composer');
    expect(textarea!.hasAttribute('disabled')).toBe(false);
    expect(el.querySelector('.composer-locked-hint')).toBeNull();
  });

  it('#1283 P2-3：整卡跳过（投影成非 null answer）⇒ 锁解除', async () => {
    const el = await mount('ask_user', { answer: 'skipped' });
    const textarea = el.querySelector('textarea.composer');
    expect(textarea!.hasAttribute('disabled')).toBe(false);
    expect(el.querySelector('.composer-locked-hint')).toBeNull();
  });
});

describe('#1224 — 卡片超时提示与后端注入消息数值一致', () => {
  it('快照 600 秒 ⇒ 底部提示渲染 10 分钟（与后端注入消息同数）', async () => {
    const el = await mount('ask_user', { seconds: 600, minutes: 10 });
    const tip = el.querySelector('.ask-user-timeout-tip')?.textContent ?? '';
    expect(tip).toBe('10 分钟无回答自动继续');
  });

  it('快照 60 秒 ⇒ 底部提示渲染 1 分钟（下界，不是 0）', async () => {
    const el = await mount('ask_user', { seconds: 60, minutes: 1 });
    const tip = el.querySelector('.ask-user-timeout-tip')?.textContent ?? '';
    expect(tip).toBe('1 分钟无回答自动继续');
  });

  it('旧事件只有 seconds 字段 ⇒ 仍按同一公式换算（60 → 1 分钟）', async () => {
    const el = await mount('ask_user', { seconds: 60 });
    const tip = el.querySelector('.ask-user-timeout-tip')?.textContent ?? '';
    expect(tip).toBe('1 分钟无回答自动继续');
  });

  it('快照缺失（旧事件）⇒ 中性提示，不伪造数字', async () => {
    const el = await mount('ask_user');
    const tip = el.querySelector('.ask-user-timeout-tip')?.textContent ?? '';
    expect(tip).toBe('长时间未回答将自动继续');
  });

  it.each([0, -60, 1.5, 600.5])(
    '非法快照值 %s ⇒ 中性提示（零伪造，不钳成 1 分钟）',
    async (bad) => {
      const el = await mount('ask_user', { seconds: bad, minutes: bad });
      const tip = el.querySelector('.ask-user-timeout-tip')?.textContent ?? '';
      expect(tip).toBe('长时间未回答将自动继续');
    },
  );
});
