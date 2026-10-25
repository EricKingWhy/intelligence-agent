// @vitest-environment jsdom
/** #1148（分离投递迟到失败丢文本）数据层契约：判定窗口**之外**落定的失败，
 *  必须把原文经 `failedDraft` 交回 Composer。
 *
 *  两条路径（票面「症状」的两条）：
 *   A) `sendFollowUp` 窗外落定：待裁决 409 在吐响应头之前要先在后端把 pending 裁决
 *      查出来（`resume_and_launch` → `recover`），冷启动 / 慢盘越过 1200ms 完全正常。
 *      一旦越窗，hook 已按"已受理"返回 ⇒ Composer 乐观清空草稿，而迟到的失败原先
 *      只剩一条横幅（`setError`），**原文没有任何回填路径**。
 *   B) `submitTask` 认领成功后 POST 才 reject：会话确实建出来了，但这条消息有没有随
 *      POST 送出去是另一回事；`late.failure` 写进去却只在认领抛错时被读一次。
 *
 *  手法同 `useSession.followUpRecovery.test.tsx` / `useSession.pendingDecisions.test.tsx`：
 *  只 mock 传输层，hook 本体 / 分派表 / 投影全走真实代码路径。
 */

import { act, createElement, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentEvent } from '../types';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listSessions: vi.fn(async () => []),
    getSessionEvents: vi.fn(async () => []),
    listSessionQueue: vi.fn(async () => ({ items: [], steers: [] })),
    sendMessage: vi.fn(),
    startSession: vi.fn(),
  };
});

// ⚠ `sessionIdBaselineDetailed` / `discoverNewSessionId` 在 `lib/wsStream`，**不在** `lib/api`。
vi.mock('../lib/wsStream', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/wsStream')>();
  return {
    ...actual,
    // #1333：useSession 改取 detailed 基线（含服务端时钟锚点）；serverNowMs
    // 给 null 走客户端时刻降级（本单测不考时钟锚点）。
    sessionIdBaselineDetailed: vi.fn(async () => ({ ids: new Set<string>(), serverNowMs: null })),
    discoverNewSessionId: vi.fn(),
  };
});

import { getSessionEvents, sendMessage as apiSendMessage, startSession, type PendingDecision } from '../lib/api';
import { SessionClaimAmbiguousError, SessionClaimTimeoutError, discoverNewSessionId, sessionIdBaselineDetailed } from '../lib/wsStream';
import { EARLY_RESPONSE_WINDOW_MS, useSession } from './useSession';

const SID = 's-1148';

/** 已收口会话历史（不开实时流，聚焦发送路径本身）。 */
const idleHistory = (sid: string): AgentEvent[] =>
  [
    { type: 'session/started', seq: 1, session_id: sid, data: {}, run_id: null, step_id: null },
    { type: 'run/completed', seq: 2, session_id: sid, data: {}, run_id: 'r-1', step_id: null },
  ] as AgentEvent[];

const PENDING: PendingDecision[] = [
  {
    tool_call_id: 'call_1148',
    tool_name: 'bash',
    state: 'UNKNOWN',
    default_action: 'DEFER',
    risk_level: 'high',
    probe: { verifiable: false, suggested_action: null },
  },
];

/** 后端 409 的结构化 detail（待裁决清单非空 ⇒ 走 recovery-conflict 支）。 */
const conflict409 = (): Response =>
  new Response(
    JSON.stringify({ detail: { message: '先 POST /api/sessions/s-1148/recover 结清', pending_decisions: PENDING } }),
    { status: 409, headers: { 'content-type': 'application/json' } },
  );

/** 受理收据（JSON = queued/steered ack）。 */
const queuedOk = (): Response =>
  new Response(JSON.stringify({ status: 'queued', queue_id: 'q-1' }), {
    status: 200, headers: { 'content-type': 'application/json' },
  });

type SessionApi = ReturnType<typeof useSession>;
const captured = { hook: null as SessionApi | null };
let container: HTMLDivElement | null = null;
let root: Root | null = null;

function Harness() {
  const api = useSession();
  useEffect(() => { captured.hook = api; });
  return null;
}

async function mount(): Promise<void> {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => { root!.render(createElement(Harness)); });
  await act(async () => {});
  if (!captured.hook) throw new Error('harness 未捕获到 hook');
}

async function enterViewing(sid = SID): Promise<void> {
  await act(async () => { captured.hook!.selectSession(sid); });
  await act(async () => {});
  expect(captured.hook!.conversation?.session_id).toBe(sid);
}

/** 推过判定窗口（1200ms）：此后 `sendFollowUp` 已按"已受理"返回，不再等这条响应。
 *  **顺序是承重的**：先起请求、再推进计时器、最后才 await（反过来会死锁——那条
 *  promise 要等窗口计时器落定，而计时器只在 await 之后才被推进）。 */
async function driveOutOfWindow(action: () => Promise<unknown>): Promise<unknown> {
  let pending: Promise<unknown> | undefined;
  let outcome: unknown;
  await act(async () => {
    pending = action();
    await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
    outcome = await pending;
  });
  return outcome;
}

/** 一个"响应头晚到"的响应：由测试决定何时落定。 */
function lateResponse(): { promise: Promise<Response>; settle: (r: Response) => void } {
  let settle!: (r: Response) => void;
  const promise = new Promise<Response>((resolve) => { settle = resolve; });
  return { promise, settle };
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  vi.useFakeTimers();
  vi.mocked(getSessionEvents).mockReset();
  vi.mocked(getSessionEvents).mockResolvedValue(idleHistory(SID));
  vi.mocked(apiSendMessage).mockReset();
  vi.mocked(startSession).mockReset();
  vi.mocked(sessionIdBaselineDetailed).mockReset();
  vi.mocked(sessionIdBaselineDetailed).mockResolvedValue({ ids: new Set<string>(), serverNowMs: null });
  vi.mocked(discoverNewSessionId).mockReset();
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  container?.remove();
  container = null;
  root = null;
  captured.hook = null;
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('#1148 路径 A — sendFollowUp 窗外落定的失败', () => {
  it('AC1：窗外落定的 409 → failedDraft 拿到原文（不是只设一条横幅）', async () => {
    const late = lateResponse();
    vi.mocked(apiSendMessage).mockReturnValue(late.promise);
    await mount();
    await enterViewing();

    // 推过窗口：此刻 sendFollowUp 已按"已受理"返回（无法 await 结局）。
    const accepted = await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '窗外失败也必须留住'));
    expect(accepted).toBe(true);
    expect(captured.hook!.failedDraft).toBeNull(); // 还没落定 ⇒ 不能预先报错

    await act(async () => { late.settle(conflict409()); });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft?.text).toBe('窗外失败也必须留住');
    // 两条通道各司其职：人话横幅 + 清单进裁决面板（既有语义不变）。
    expect(captured.hook!.error).toContain('需要你来决定');
    expect(captured.hook!.recoverState.pendingDecisions).toEqual(PENDING);
  });

  it('AC1：窗外被拒绝（网络级失败）→ 同样交出原文', async () => {
    let reject!: (e: Error) => void;
    const promise = new Promise<Response>((_res, rej) => { reject = rej; });
    vi.mocked(apiSendMessage).mockReturnValue(promise);
    await mount();
    await enterViewing();

    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '断网也要留住'));
    await act(async () => { reject(new Error('network down')); });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft?.text).toBe('断网也要留住');
    expect(captured.hook!.error).toContain('续聊失败');
  });

  it('AC4 数据层：同一段文本连续失败两次 → 每次都是新的回执对象', async () => {
    // 裸 string 会被 React 的 `Object.is` 挡掉：状态相等 ⇒ 不重渲染 ⇒ effect 不触发。
    const first = lateResponse();
    vi.mocked(apiSendMessage).mockReturnValueOnce(first.promise);
    await mount();
    await enterViewing();

    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '同一段'));
    await act(async () => { first.settle(conflict409()); });
    await act(async () => { await Promise.resolve(); });
    const firstReceipt = captured.hook!.failedDraft;
    expect(firstReceipt?.text).toBe('同一段');

    const second = lateResponse();
    vi.mocked(apiSendMessage).mockReturnValueOnce(second.promise);
    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '同一段'));
    await act(async () => { second.settle(conflict409()); });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft?.text).toBe('同一段');
    expect(captured.hook!.failedDraft).not.toBe(firstReceipt);
  });

  it('反证：窗外落定的是**成功**（queued 收据）→ 不产生失败回执（不误报）', async () => {
    const late = lateResponse();
    vi.mocked(apiSendMessage).mockReturnValue(late.promise);
    await mount();
    await enterViewing();

    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '其实成功了'));
    await act(async () => { late.settle(queuedOk()); });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft).toBeNull();
  });

  it('用户在迟到落定前切走会话 → 不把旧会话的原文倒进新会话（代际闸）', async () => {
    const late = lateResponse();
    vi.mocked(apiSendMessage).mockReturnValue(late.promise);
    await mount();
    await enterViewing();

    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '属于旧会话的文本'));
    await act(async () => { captured.hook!.selectSession('s-2'); });
    await act(async () => {});
    await act(async () => { late.settle(conflict409()); });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft).toBeNull();
  });

  it('下一次投递清掉旧回执 → Composer 重挂（切页签）不会复活已重发的原文', async () => {
    const late = lateResponse();
    vi.mocked(apiSendMessage).mockReturnValueOnce(late.promise);
    await mount();
    await enterViewing();

    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '失败过一次的原文'));
    await act(async () => { late.settle(conflict409()); });
    await act(async () => { await Promise.resolve(); });
    expect(captured.hook!.failedDraft?.text).toBe('失败过一次的原文');

    // 用户重发（这一次成功）：回执必须随之作废。
    const ok = lateResponse();
    vi.mocked(apiSendMessage).mockReturnValueOnce(ok.promise);
    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '失败过一次的原文'));
    await act(async () => { ok.settle(queuedOk()); });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft).toBeNull();
  });
});

describe('#1148 路径 B — submitTask 认领成功后 POST 才失败', () => {
  /** 造出「认领成功、随后 POST 才 reject」的序列，reject 相对认领的**微任务偏移**
   *  可调（`offset` ≥ 0 表示 reject 比认领晚 offset 个微任务）。
   *
   *  ⚠ 这个 offset 维度是承重的：第一版修复用「读一次 `late.failure` 快照」判定，
   *  只在 offset ≤ 0 时命中（晚一个微任务就 MISS）。只测 offset=0 的用例会给人
   *  "已守住"的错觉。 */
  async function driveClaimThenReject(
    task: string,
    offset = 0,
    deferClaim = false,
    settlePost = true,
  ): Promise<{ result: string | null; postUnsettled: boolean }> {
    let rejectPost!: (e: Error) => void;
    let postUnsettled = true;
    const post = new Promise<Response>((_resolve, reject) => {
      rejectPost = (e: Error) => { postUnsettled = false; reject(e); };
    });
    vi.mocked(startSession).mockReturnValue(post as never);

    let releaseClaim!: () => void;
    const claimGate = new Promise<void>((resolve) => { releaseClaim = resolve; });
    vi.mocked(discoverNewSessionId).mockImplementation(async () => {
      if (deferClaim) await claimGate;
      return 's-new';
    });

    let result: string | null = null;
    await act(async () => {
      const p = captured.hook!.submitTask({ task });
      await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
      if (deferClaim) releaseClaim();
      if (settlePost) {
        for (let i = 0; i < offset; i++) await Promise.resolve();
        rejectPost(new Error('网络中断：POST /api/sessions'));
      }
      await vi.advanceTimersByTimeAsync(0);
      result = await p;
    });
    return { result, postUnsettled };
  }

  it('AC2：POST 在认领当刻已失败（offset=0）⇒ 原文必须回到用户手里', async () => {
    await mount();
    const { result } = await driveClaimThenReject('新建会话里也要留住的文本', 0, true);

    // 两条合法出路：① 返回失败原因 ⇒ Composer 不清空；② 返回 null（已接管）但把
    // 失败经 failedDraft 交回 ⇒ Composer 回填。不论走哪条，用户都必须拿到原文。
    expect(result !== null || captured.hook!.failedDraft !== null).toBe(true);
    if (captured.hook!.failedDraft !== null) {
      expect(captured.hook!.failedDraft.text).toBe('新建会话里也要留住的文本');
    } else {
      expect(result).toContain('网络中断');
    }
  });

  it('**承重**：reject 比认领晚 1..4 个微任务也绝不能丢文本（快照写法在此全漏）', async () => {
    for (const offset of [1, 2, 3, 4]) {
      await mount();
      const { result } = await driveClaimThenReject(`迟到失败 offset=${offset}`, offset, true);

      const recovered = result !== null || captured.hook!.failedDraft !== null;
      expect(
        recovered,
        `offset=${offset} 必须有一条失败出路，实测 result=${String(result)} / failedDraft=${JSON.stringify(captured.hook!.failedDraft)}`,
      ).toBe(true);
      const text = result !== null ? result : captured.hook!.failedDraft!.text;
      expect(text).toContain(`offset=${offset}`);
      await act(async () => { root!.unmount(); });
      container?.remove(); root = null; container = null; captured.hook = null;
    }
  }, 30000);

  it('反证：POST 仍在途（未 reject）时认领成功 ⇒ 仍按"已接管"返回 null，不误报失败', async () => {
    // 守卫最容易过头的方向：正常流式的响应头要等 run 结束才到，认领成功时 POST 必然
    // 还悬着。若把"还悬着"也当失败，用户每次新建会话都会被误报成失败。
    await mount();
    const { result, postUnsettled } = await driveClaimThenReject('正常新建', 0, false, false);

    expect(postUnsettled).toBe(true);
    expect(result).toBeNull();
    expect(captured.hook!.failedDraft).toBeNull();
  });

  it('AC5：ownError 提交（TaskCreationDialog）迟到失败 ⇒ 不把弹窗文本灌进 Composer', async () => {
    let rejectPost!: (e: Error) => void;
    const post = new Promise<Response>((_r, reject) => { rejectPost = reject; });
    vi.mocked(startSession).mockReturnValue(post as never);
    vi.mocked(discoverNewSessionId).mockResolvedValue('s-new');

    await mount();
    await act(async () => {
      const p = captured.hook!.submitTask({ task: '弹窗里的任务文本' }, { ownError: true });
      await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
      await p;
    });
    await act(async () => {
      rejectPost(new Error('网络中断'));
      await vi.advanceTimersByTimeAsync(0);
      await Promise.resolve(); await Promise.resolve();
    });

    // 既不能灌草稿，也不能打全局横幅（横幅由调用方就地展示）。
    expect(captured.hook!.failedDraft).toBeNull();
    expect(captured.hook!.error).toBeNull();
  });
});

describe('#1321 AC4 — 认领超时不得被渲染成「提交失败」', () => {
  it('会话可能已创建但未接上 ⇒ 可行动提示 + 保留原文', async () => {
    // POST 仍悬挂（交付层攒包 / 后端慢），认领在预算内没认出会话。
    vi.mocked(startSession).mockReturnValue(new Promise<Response>(() => {}) as never);
    vi.mocked(discoverNewSessionId).mockRejectedValue(new SessionClaimTimeoutError());

    await mount();
    let result: string | null = null;
    await act(async () => {
      const p = captured.hook!.submitTask({ task: '慢负载下不能丢的原文' });
      await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
      result = await p;
    });

    // 返回非 null = Composer 按「未受理」保留输入框原文。
    expect(result).not.toBeNull();
    expect(captured.hook!.error).not.toContain('提交失败');
    expect(captured.hook!.error).toContain('可能已成功');
    expect(captured.hook!.error).toContain('刷新');
  });

  it('认领期 POST 迟到失败 ⇒ 同样按「结局不明」呈现，且不给重试动作（审查 P1-1）', async () => {
    let rejectPost!: (e: Error) => void;
    vi.mocked(startSession).mockReturnValue(
      new Promise<Response>((_r, reject) => { rejectPost = reject; }) as never,
    );
    let rejectClaim!: (e: Error) => void;
    vi.mocked(discoverNewSessionId).mockReturnValue(
      new Promise<string>((_r, reject) => { rejectClaim = reject; }) as never,
    );

    await mount();
    let result: string | null = null;
    await act(async () => {
      const p = captured.hook!.submitTask({ task: 'POST 真的失败了' }, { onRetry: () => {} });
      await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
      // 先让 POST 落定失败，再由认领超时收口：`late.failure` 必须优先。
      rejectPost(new Error('网络中断'));
      await vi.advanceTimersByTimeAsync(0);
      rejectClaim(new SessionClaimTimeoutError());
      await vi.advanceTimersByTimeAsync(0);
      result = await p;
    });

    // 认领已经越过分叉窗口 ⇒ 服务端很可能已落盘 ⇒ 不能自称「提交失败」并劝重试。
    expect(result).toContain('可能已成功');
    expect(result).toContain('网络中断');
    expect(captured.hook!.sendFailure?.retryable).toBe(false);
    expect(captured.hook!.sendFailure?.onRetry).toBeUndefined();
  });
});

describe('#1333 findings 修复——认领接线', () => {
  const T0 = 1_750_000_000_000;

  it('P0 接线：submittedAtServerMs 钉在 POST 发起时刻（早窗之前），不是认领开始', async () => {
    // 服务端时钟锚点：基线 Date 头 = T0
    vi.mocked(sessionIdBaselineDetailed).mockResolvedValue({
      ids: new Set<string>(),
      serverNowMs: T0,
    });
    vi.mocked(startSession).mockReturnValue(new Promise<Response>(() => {}) as never);
    vi.mocked(discoverNewSessionId).mockRejectedValue(new SessionClaimTimeoutError());

    await mount();
    let result: string | null = null;
    await act(async () => {
      const p = captured.hook!.submitTask({ task: '接线钉锚点' });
      await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
      result = await p;
    });

    const opts = vi.mocked(discoverNewSessionId).mock.calls[0]?.[1];
    // fake timers 下基线返回与 POST 发起是同一刻度：锚点恰为 T0。
    // 若锚点被挪到认领开始（1200ms 早窗之后）才算，这里会是 T0 + 1200——
    // 这正是旧 bug 让"自己的会话每轮被滤掉"的原因。
    expect(opts?.submittedAtServerMs).toBe(T0);
    expect(result).toContain('可能已成功');
  });

  it('C-P3-1（#1367 P1-1 修订）：POST 的迟到失败优先于认领歧义，但呈现为「结局不明」', async () => {
    let rejectPost!: (e: Error) => void;
    vi.mocked(startSession).mockReturnValue(
      new Promise<Response>((_r, reject) => { rejectPost = reject; }) as never,
    );
    let rejectClaim!: (e: Error) => void;
    vi.mocked(discoverNewSessionId).mockReturnValue(
      new Promise<string>((_r, reject) => { rejectClaim = reject; }) as never,
    );

    await mount();
    let result: string | null = null;
    await act(async () => {
      const p = captured.hook!.submitTask({ task: 'POST 失败盖住歧义' });
      await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
      // 先让 POST 落定失败，再抛歧义：`late.failure ?? e` 必须是前者赢——
      // 失败原因的优先级不变（POST 的硬失败比认领结论更硬），但 #1367 P1-1 修订了
      // 呈现：认领期已越过分叉窗口 ⇒ 服务端很可能已落盘 ⇒ 不能自称「提交失败」并劝重试。
      rejectPost(new Error('401 鉴权失败'));
      await vi.advanceTimersByTimeAsync(0);
      rejectClaim(new SessionClaimAmbiguousError(['other-0', 'fresh-0']));
      await vi.advanceTimersByTimeAsync(0);
      result = await p;
    });

    // 原因仍是 POST 的（不是歧义的「无法确认」），但文案是「可能已成功」且不可重试。
    expect(result).toContain('可能已成功');
    expect(result).toContain('401 鉴权失败');
    expect(result).not.toContain('无法确认');
    expect(captured.hook!.error).toContain('可能已成功');
    expect(captured.hook!.sendFailure?.retryable).toBe(false);
  });
});

describe('#1333 client_request_id 接线：每次 POST 新 UUID，随创建请求与认领下发', () => {
  it('submitTask 生成 UUID v4 传给 startSession 与 discoverNewSessionId（两次提交不同值）', async () => {
    vi.mocked(sessionIdBaselineDetailed).mockResolvedValue({
      ids: new Set<string>(),
      serverNowMs: null,
    });
    vi.mocked(startSession).mockReturnValue(new Promise<Response>(() => {}) as never);
    vi.mocked(discoverNewSessionId).mockRejectedValue(new SessionClaimTimeoutError());

    await mount();
    const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
    await act(async () => {
      const p = captured.hook!.submitTask({ task: '第一次提交' });
      await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
      await p;
    });
    const firstStart = vi.mocked(startSession).mock.calls[0];
    const firstClaim = vi.mocked(discoverNewSessionId).mock.calls[0]?.[1] as
      | { clientRequestId?: string }
      | undefined;
    expect(firstStart?.[1]?.clientRequestId).toMatch(uuidV4);
    expect(firstClaim?.clientRequestId).toBe(firstStart?.[1]?.clientRequestId);

    await act(async () => {
      const p = captured.hook!.submitTask({ task: '第二次提交' });
      await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
      await p;
    });
    const secondStart = vi.mocked(startSession).mock.calls[1];
    const secondClaim = vi.mocked(discoverNewSessionId).mock.calls[1]?.[1] as
      | { clientRequestId?: string }
      | undefined;
    expect(secondStart?.[1]?.clientRequestId).toMatch(uuidV4);
    expect(secondStart?.[1]?.clientRequestId).not.toBe(firstStart?.[1]?.clientRequestId);
    expect(secondClaim?.clientRequestId).toBe(secondStart?.[1]?.clientRequestId);
  });

  it('非安全上下文（crypto.randomUUID 缺失）→ getRandomValues 回退仍产出 v4，提交不被 TypeError 打断', async () => {
    // 裸 HTTP 绑定非 loopback 地址（不受支持部署形态）下 randomUUID 为
    // undefined——审查 P2：回退必须保证 submitTask 照常发出 POST。
    const realGetRandomValues = crypto.getRandomValues.bind(crypto);
    vi.stubGlobal('crypto', { getRandomValues: realGetRandomValues });
    try {
      vi.mocked(sessionIdBaselineDetailed).mockResolvedValue({
        ids: new Set<string>(),
        serverNowMs: null,
      });
      vi.mocked(startSession).mockReturnValue(new Promise<Response>(() => {}) as never);
      vi.mocked(discoverNewSessionId).mockRejectedValue(new SessionClaimTimeoutError());

      await mount();
      await act(async () => {
        const p = captured.hook!.submitTask({ task: '无 randomUUID 环境' });
        await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
        await p;
      });
      const started = vi.mocked(startSession).mock.calls[0];
      expect(started?.[1]?.clientRequestId).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
