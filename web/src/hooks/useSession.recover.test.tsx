// @vitest-environment jsdom
/** #357 W-13 数据层（第一批）：useSession 的 409 裁决清单接入 + 提交续跑。
 *
 * 覆盖：
 *   - recover 409 → conflict 态携带后端 pending_decisions（含 default_action/risk_level/probe）；
 *   - defaultDecisionDrafts：裁决清单只来自后端，默认 verdict 取自 default_action（#22）；
 *   - submitDecisions 提交后重调 recover（同一 decisions 载荷）→ 200 走既有 done 重建；
 *   - 提交期间切走 → 晚到结果丢弃（shouldApplyRecoverResult 守护不变）；
 *   - 重复提交（同 decisions 发两次）→ 两次请求载荷一致。
 *
 * 手法：mock api 层（recoverSession 归测试驱动，getSessionEvents 读 holder），
 * useSession 本体 / 投影 / 守护全走真实代码路径；与 useSession.externalRun.test.tsx
 * 同款 createRoot 夹具。
 */

import { act, createElement, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentEvent } from '../types';

const apiState = vi.hoisted(() => ({ events: [] as AgentEvent[] }));

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listSessions: vi.fn(async () => []),
    getSessionEvents: vi.fn(async () => apiState.events),
    listSessionQueue: vi.fn(async () => ({ items: [], steers: [] })),
    recoverSession: vi.fn(),
  };
});

import {
  RecoverError,
  recoverSession,
  type PendingDecision,
  type RecoverDecisionInput,
} from '../lib/api';
import { defaultDecisionDrafts, useSession } from './useSession';

const SID = 's-1';
const SID2 = 's-2';

/** 已收口会话历史（不开实时流，聚焦 recover 本身）。 */
const idleHistory = (sid: string): AgentEvent[] =>
  [
    { type: 'session/started', seq: 1, session_id: sid, data: {}, run_id: null, step_id: null },
    { type: 'run/completed', seq: 2, session_id: sid, data: {}, run_id: 'r-1', step_id: null },
  ] as AgentEvent[];

/** recover 200 返回：与 GET events 同构的全量事件。 */
const recoverEvents = (sid = SID): AgentEvent[] =>
  [
    { type: 'session/started', seq: 1, session_id: sid, data: {}, run_id: null, step_id: null },
    { type: 'run/completed', seq: 2, session_id: sid, data: {}, run_id: 'r-1', step_id: null },
    { type: 'tool/result', seq: 3, session_id: sid, data: { tool_call_id: 'call_1' }, run_id: 'r-1', step_id: 1 },
  ] as AgentEvent[];

const PENDING: PendingDecision[] = [
  {
    tool_call_id: 'call_1',
    tool_name: 'read',
    state: 'RUNNING',
    default_action: 'RETRY',
    risk_level: 'low',
    probe: { verifiable: true, suggested_action: '重读文件确认' },
  },
  {
    tool_call_id: 'call_2',
    tool_name: 'bash',
    state: 'UNKNOWN',
    default_action: 'DEFER',
    risk_level: 'high',
    probe: { verifiable: false, suggested_action: null },
  },
];

type SessionApi = ReturnType<typeof useSession>;
const captured = { hook: null as SessionApi | null };
let container: HTMLDivElement | null = null;
let root: Root | null = null;

function Harness() {
  const api = useSession();
  useEffect(() => {
    captured.hook = api;
  });
  return null;
}

async function mount(): Promise<void> {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(createElement(Harness));
  });
}

async function enterViewing(sid = SID): Promise<void> {
  await act(async () => {
    captured.hook!.selectSession(sid);
  });
  await act(async () => {});
  expect(captured.hook!.conversation?.session_id).toBe(sid);
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  vi.useFakeTimers();
  apiState.events = idleHistory(SID);
  vi.mocked(recoverSession).mockReset();
  vi.mocked(recoverSession).mockResolvedValue(recoverEvents());
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  container?.remove();
  root = null;
  container = null;
  captured.hook = null;
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('#357 useSession · 409 裁决清单接入（契约 2/4）', () => {
  it('recover 409 → conflict 态携带后端 pending_decisions（逐字段）', async () => {
    vi.mocked(recoverSession).mockRejectedValueOnce(new RecoverError(409, '需人工裁决', PENDING));
    await mount();
    await enterViewing(SID);

    await act(async () => {
      await captured.hook!.recover(SID);
    });

    const st = captured.hook!.recoverState;
    expect(st.status).toBe('error');
    expect(st.conflict).toBe(true);
    expect(st.message).toBe('需人工裁决');
    expect(st.pendingDecisions).toEqual(PENDING);
  });

  it('非 409 失败：pendingDecisions 恒为 null（不残留上一次清单）', async () => {
    vi.mocked(recoverSession).mockRejectedValueOnce(new RecoverError(404, '会话不存在'));
    await mount();
    await enterViewing(SID);

    await act(async () => {
      await captured.hook!.recover(SID);
    });

    const st = captured.hook!.recoverState;
    expect(st.status).toBe('error');
    expect(st.conflict).toBe(false);
    expect(st.pendingDecisions).toBeNull();
  });

  it('defaultDecisionDrafts：默认 verdict 逐条取自默认动作（顺序保持、不带 source）', () => {
    expect(defaultDecisionDrafts(PENDING)).toEqual([
      { tool_call_id: 'call_1', verdict: 'RETRY' },
      { tool_call_id: 'call_2', verdict: 'DEFER' },
    ]);
  });
});

describe('#357 useSession · submitDecisions 提交续跑（契约 4）', () => {
  it('提交后重调 recover：同一 decisions 载荷 + 200 走既有 done 重建', async () => {
    await mount();
    await enterViewing(SID);

    const decisions: RecoverDecisionInput[] = [
      { tool_call_id: 'call_1', verdict: 'RETRY' },
      { tool_call_id: 'call_2', verdict: 'DEFER', source: '我查了外部系统' },
    ];

    await act(async () => {
      await captured.hook!.submitDecisions(SID, decisions);
    });

    expect(vi.mocked(recoverSession)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(recoverSession)).toHaveBeenCalledWith(SID, decisions);
    const st = captured.hook!.recoverState;
    expect(st.status).toBe('done');
    // 成功落地后不再携带待裁决清单
    expect(st.pendingDecisions).toBeNull();
    // 200 走同一 projectHistory 重建管线（tool/result 进入视图）
    expect(captured.hook!.conversation?.events.some((e) => e.type === 'tool/result')).toBe(true);
  });

  it('重复提交（同 decisions 发两次）→ 两次请求载荷逐字一致', async () => {
    await mount();
    await enterViewing(SID);

    const decisions: RecoverDecisionInput[] = [{ tool_call_id: 'call_1', verdict: 'DEFER' }];
    await act(async () => {
      await captured.hook!.submitDecisions(SID, decisions);
    });
    await act(async () => {
      await captured.hook!.submitDecisions(SID, decisions);
    });

    expect(vi.mocked(recoverSession)).toHaveBeenCalledTimes(2);
    expect(vi.mocked(recoverSession).mock.calls[0]).toEqual([SID, decisions]);
    expect(vi.mocked(recoverSession).mock.calls[1]).toEqual([SID, decisions]);
  });

  it('提交仍被 409 拒（未覆盖全/非法）→ 刷新权威 pendingDecisions，不伪造成功', async () => {
    vi.mocked(recoverSession).mockRejectedValueOnce(
      new RecoverError(409, '仍有待裁决项', PENDING),
    );
    await mount();
    await enterViewing(SID);

    await act(async () => {
      await captured.hook!.submitDecisions(SID, [{ tool_call_id: 'call_1', verdict: 'RETRY' }]);
    });

    const st = captured.hook!.recoverState;
    expect(st.status).toBe('error');
    expect(st.conflict).toBe(true);
    expect(st.pendingDecisions).toEqual(PENDING);
  });

  it('提交期间切走 → 晚到的 200 结果丢弃（shouldApplyRecoverResult 守护不变）', async () => {
    let release: (events: AgentEvent[]) => void = () => {};
    vi.mocked(recoverSession).mockImplementationOnce(
      () => new Promise<AgentEvent[]>((resolve) => { release = resolve; }),
    );
    await mount();
    await enterViewing(SID);

    let pending: Promise<void> = Promise.resolve();
    await act(async () => {
      pending = captured.hook!.submitDecisions(SID, [{ tool_call_id: 'call_1', verdict: 'RETRY' }]);
    });

    // pending 期间切到别的会话
    await act(async () => {
      captured.hook!.selectSession(SID2);
    });

    await act(async () => {
      release(recoverEvents());
      await pending;
    });

    // 结果不得落到已切走的会话：不能是 done
    expect(captured.hook!.recoverState.status).not.toBe('done');
  });
});
