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

import { getSessionRecovery, NotFoundError, recoverSession, type PendingDecision } from '../lib/api';
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
  vi.mocked(recoverSession).mockReset();
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

// ── #1030 审查 P1（轴 A）：DEFER 裁决后徽标回落成「思考中」──
//
// 后端契约（`recovery/reconcile.py:71-74` + `tests/test_web_api.py::test_recover_with_defer_is_accepted_and_keeps_pending`）：
// `DEFER` **不解封**——Ledger 保持 NEED_RECONCILE，该调用"仍出现在后续
// pending_decisions 里"。所以 POST /recover 的 **200 只证明请求被接受，不证明裁决已结清**。
//
// 修前：200 分支无条件写 `pendingDecisions: null`，而主动读面只依赖 `[mode]`、提交成功
// 不改 mode ⇒ 没有任何东西重读 ⇒ `hasPendingDecisions` 转假 ⇒ 面板与横幅双双消失、
// 徽标回到「思考中」，而会话其实仍被封着。这正是本票（AC1/AC4）要消灭的谎报。
//
// 加重因素：`default_action` 由工具的 `replay_safe` 决定，`bash` 未覆写（默认 False）
// ⇒ 票面现场那个 bash 调用的默认档**就是 DEFER**。用户"照默认走"一次即复现。

describe('#1030 审查修复：裁决 200 之后必须重读清单（DEFER 不解封）', () => {
  /** 200 返回：与 GET events 同构的全量事件（recover 的成功载荷）。 */
  const okEvents = (sid = SID): AgentEvent[] =>
    [
      { type: 'session/started', seq: 1, session_id: sid, data: {}, run_id: null, step_id: null },
      { type: 'run/started', seq: 2, session_id: sid, data: {}, run_id: 'r-1', step_id: null },
      { type: 'tool/call', seq: 3, session_id: sid, data: { tool_call_id: 'call_1', tool_name: 'bash' }, run_id: 'r-1', step_id: 1 },
      { type: 'run/interrupted', seq: 4, session_id: sid, data: { reason: 'process_restart' }, run_id: 'r-1', step_id: null },
    ] as AgentEvent[];

  it('DEFER 提交成功（200）后清单必须**重读**，不得凭"POST 成功"推断已结清', async () => {
    vi.mocked(getSessionRecovery).mockResolvedValue(PENDING);
    await mount();
    await enterViewing();
    expect(captured.hook!.recoverState.pendingDecisions).toEqual(PENDING);
    const readsBefore = vi.mocked(getSessionRecovery).mock.calls.length;

    // 后端对 DEFER 的回应：200 + 该 op 仍在 pending（不解封）。
    vi.mocked(recoverSession).mockResolvedValueOnce(okEvents());
    await act(async () => {
      await captured.hook!.submitDecisions(SID, [{ tool_call_id: 'call_1', verdict: 'DEFER' }]);
    });

    // 关键：200 之后必须再读一次——否则界面会把"还没结清"说成"没事了"。
    expect(
      vi.mocked(getSessionRecovery).mock.calls.length,
      'DEFER 200 之后没有重读清单',
    ).toBeGreaterThan(readsBefore);
    // 重读拿到的事实仍是"有待裁决" ⇒ 清单不得被清成 null/[]。
    expect(captured.hook!.recoverState.pendingDecisions).toEqual(PENDING);
  });

  it('ABANDON 提交成功（200）后重读拿到空清单 ⇒ 如实转为"已结清"', async () => {
    // 与上一条成对：修复不能变成"永远不清"——ABANDON 真的解封（后端映射
    // OperationState.CANCELLED），此时重读会拿到 []，界面必须如实收口。
    vi.mocked(getSessionRecovery).mockResolvedValue(PENDING);
    await mount();
    await enterViewing();

    vi.mocked(recoverSession).mockResolvedValueOnce(okEvents());
    vi.mocked(getSessionRecovery).mockResolvedValue([]);
    await act(async () => {
      await captured.hook!.submitDecisions(SID, [{ tool_call_id: 'call_1', verdict: 'ABANDON' }]);
    });

    expect(captured.hook!.recoverState.pendingDecisions).toEqual([]);
  });

  it('重读失败时保持"未知"（null），不回落空清单——"读不到"仍 ≠ "没事"', async () => {
    vi.mocked(getSessionRecovery).mockResolvedValue(PENDING);
    await mount();
    await enterViewing();
    const readsBefore = vi.mocked(getSessionRecovery).mock.calls.length;

    vi.mocked(recoverSession).mockResolvedValueOnce(okEvents());
    vi.mocked(getSessionRecovery).mockRejectedValue(new Error('boom'));
    await act(async () => {
      await captured.hook!.submitDecisions(SID, [{ tool_call_id: 'call_1', verdict: 'DEFER' }]);
    });

    // 先断言"确实重读了"——否则本用例在修前修后都得 null，证明不了任何事。
    expect(
      vi.mocked(getSessionRecovery).mock.calls.length,
      'DEFER 200 之后没有重读清单',
    ).toBeGreaterThan(readsBefore);
    expect(captured.hook!.recoverState.pendingDecisions).toBeNull();
  });

  it('切走会话后晚到的重读结果不写进新会话（stale-write 守护）', async () => {
    vi.mocked(getSessionRecovery).mockResolvedValue(PENDING);
    await mount();
    await enterViewing();

    // 让裁决之后那次重读悬停（不 resolve），才能在它落地前切走。
    let release: ((v: PendingDecision[]) => void) | null = null;
    vi.mocked(getSessionRecovery).mockImplementationOnce(
      () => new Promise<PendingDecision[]>((resolve) => { release = resolve; }),
    );
    vi.mocked(recoverSession).mockResolvedValueOnce(okEvents());
    // 不 await：`recover` 的 200 分支会 await 这次重读，等它就死锁了。
    await act(async () => {
      void captured.hook!.submitDecisions(SID, [{ tool_call_id: 'call_1', verdict: 'DEFER' }]);
    });

    // 重读在途时切到别的会话 → 旧结果落地时不得污染新会话的态。
    apiState.events = idleHistory('s-2');
    vi.mocked(getSessionRecovery).mockResolvedValue([]);
    await enterViewing('s-2');
    await act(async () => { release?.(PENDING); });
    expect(captured.hook!.recoverState.pendingDecisions).toEqual([]);
  });
});
