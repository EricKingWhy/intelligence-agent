// @vitest-environment jsdom
/** #1030（缺陷②）数据层：打开会话即**主动**读取待裁决清单。
 *
 * 此前 `recoverState.pendingDecisions` 只来自一次带 `pending_decisions` 的 409，
 * 且前置还得先点「恢复会话」——把安全机制藏在错误路径里（AC3 要求"无需任何 409，
 * 用户打开待裁决会话即可看到裁决入口"）。本文件锁主动读面的四条契约：
 *
 *   1. 进入 viewing 就拉一次 `GET /api/sessions/{id}/recovery`（零点击、零 409）；
 *   2. 结果如实落到 `recoverState.pendingDecisions`（AC3 的前提）；
 *   3. **读不到 ≠ 没事**：失败时保持"未知"，绝不回落空清单（不变量 #22）；
 *   4. AC4：重启场景（run/interrupted 落盘 + 裁决仍挂）仍能读到清单——
 *      这正是票面实测的峰值误导态（横幅消失、病根仍在）。
 *
 * 手法与 useSession.recover.test.tsx 同款：mock api 层，hook 本体走真实路径。
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
    getSessionRecovery: vi.fn(async () => []),
    listSessionQueue: vi.fn(async () => ({ items: [], steers: [] })),
    recoverSession: vi.fn(),
  };
});

import { getSessionRecovery, NotFoundError, type PendingDecision } from '../lib/api';
import { useSession } from './useSession';

const SID = 's-1';

/** 已收口会话：没有未终结 run，不会走 resumeLiveStream（聚焦主动读面本身）。 */
const idleHistory = (sid: string): AgentEvent[] =>
  [
    { type: 'session/started', seq: 1, session_id: sid, data: {}, run_id: null, step_id: null },
    { type: 'run/completed', seq: 2, session_id: sid, data: {}, run_id: 'r-1', step_id: null },
  ] as AgentEvent[];

/** AC4 现场：run/interrupted **已落盘**（重启扫描补的），但 run 无正常终态。 */
const restartedHistory = (sid: string): AgentEvent[] =>
  [
    { type: 'session/started', seq: 1, session_id: sid, data: {}, run_id: null, step_id: null },
    { type: 'run/started', seq: 2, session_id: sid, data: {}, run_id: 'r-1', step_id: null },
    { type: 'run/interrupted', seq: 3, session_id: sid, data: { interrupted_seq: 2, reason: 'process_restart' }, run_id: 'r-1', step_id: null },
  ] as AgentEvent[];

const PENDING: PendingDecision[] = [
  {
    tool_call_id: 'call_1',
    tool_name: 'bash',
    state: 'UNKNOWN',
    default_action: 'RETRY',
    risk_level: 'low',
    probe: { verifiable: true, suggested_action: '重读文件确认' },
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
  apiState.events = idleHistory(SID);
  vi.mocked(getSessionRecovery).mockReset();
  vi.mocked(getSessionRecovery).mockResolvedValue([]);
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  container?.remove();
  root = null;
  container = null;
  captured.hook = null;
  vi.clearAllMocks();
});

describe('#1030 useSession · 打开会话主动读待裁决清单（AC3/AC4）', () => {
  it('AC3：进入会话即拉一次只读端点——零点击、零 409', async () => {
    await mount();
    expect(vi.mocked(getSessionRecovery)).not.toHaveBeenCalled(); // idle：无会话可查

    await enterViewing();
    expect(vi.mocked(getSessionRecovery)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(getSessionRecovery).mock.calls[0][0]).toBe(SID);
  });

  it('AC3：清单如实落到 recoverState.pendingDecisions（逐字段）', async () => {
    vi.mocked(getSessionRecovery).mockResolvedValue(PENDING);
    await mount();
    await enterViewing();
    expect(captured.hook!.recoverState.pendingDecisions).toEqual(PENDING);
  });

  it('空清单是事实：写 []（"后端说没有"），不是 null（"不知道"）', async () => {
    vi.mocked(getSessionRecovery).mockResolvedValue([]);
    await mount();
    await enterViewing();
    expect(captured.hook!.recoverState.pendingDecisions).toEqual([]);
  });

  it('读不到 ≠ 没事：请求失败时保持未知（null），绝不回落空清单（不变量 #22）', async () => {
    // 把"读不到"渲染成"没有待裁决"正是本票要消灭的谎报——空清单会点亮
    // 「会话正常」，而事实是我们根本没读到。
    vi.mocked(getSessionRecovery).mockRejectedValue(new Error('boom'));
    await mount();
    await enterViewing();
    expect(captured.hook!.recoverState.pendingDecisions).toBeNull();
  });

  it('404（会话不存在）同样保持未知，不写空清单', async () => {
    vi.mocked(getSessionRecovery).mockRejectedValue(new NotFoundError('会话不存在'));
    await mount();
    await enterViewing();
    expect(captured.hook!.recoverState.pendingDecisions).toBeNull();
  });

  it('AC4：重启态（run/interrupted 落盘 + 裁决仍挂）仍读到清单', async () => {
    // 票面实测的峰值误导：重启后徽标转「已中断」、横幅消失，但 UNKNOWN 裁决还挂着、
    // 发消息照样 409。事件流里没有这个信号——主动读面是唯一的读面。
    apiState.events = restartedHistory(SID);
    vi.mocked(getSessionRecovery).mockResolvedValue(PENDING);
    await mount();
    await enterViewing();
    expect(captured.hook!.conversation?.run_interrupted).toBeTruthy();
    expect(captured.hook!.recoverState.pendingDecisions).toEqual(PENDING);
  });

  it('换会话会重新读（清单跟着会话走，不跨会话残留）', async () => {
    vi.mocked(getSessionRecovery).mockResolvedValue(PENDING);
    await mount();
    await enterViewing();
    expect(vi.mocked(getSessionRecovery).mock.calls[0][0]).toBe(SID);

    apiState.events = idleHistory('s-2');
    vi.mocked(getSessionRecovery).mockResolvedValue([]);
    await enterViewing('s-2');
    expect(vi.mocked(getSessionRecovery).mock.calls.at(-1)?.[0]).toBe('s-2');
    expect(captured.hook!.recoverState.pendingDecisions).toEqual([]);
  });

  it('迟到的响应不覆盖已切走的会话（stale-write 守护）', async () => {
    let release: ((v: PendingDecision[]) => void) | null = null;
    vi.mocked(getSessionRecovery).mockImplementationOnce(
      () => new Promise<PendingDecision[]>((resolve) => { release = resolve; }),
    );
    await mount();
    await act(async () => { captured.hook!.selectSession(SID); });

    // 请求还没回来就切到别的会话 → 旧响应落地时不得写进新会话的态。
    apiState.events = idleHistory('s-2');
    vi.mocked(getSessionRecovery).mockResolvedValue([]);
    await enterViewing('s-2');
    await act(async () => { release?.(PENDING); });
    expect(captured.hook!.recoverState.pendingDecisions).toEqual([]);
  });

  it('主动读面不越权：它只写 pendingDecisions，不伪造 recover 的 status', async () => {
    // 读面是"发现"，不是"恢复"。把 status 写脏会让「恢复中…」/成功提示凭空出现。
    vi.mocked(getSessionRecovery).mockResolvedValue(PENDING);
    await mount();
    await enterViewing();
    expect(captured.hook!.recoverState.status).toBe('idle');
    expect(captured.hook!.recoverState.conflict).toBe(false);
  });
});
