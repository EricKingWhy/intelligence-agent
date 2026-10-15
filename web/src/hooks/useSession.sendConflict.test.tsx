// @vitest-environment jsdom
/** #1029（[Web Composer] 待裁决会话发送必丢文本）数据层契约：**发送路径**撞上
 *  待裁决 409 时，1) 如实回报"未受理"，2) 把后端的 `pending_decisions` 交给裁决面板。
 *
 *  修复前的两处根因（都在本文件可观测）：
 *  ① `sendFollowUp` 的收口 catch 只 `setError` 就咽下异常 ⇒ 返回值恒为"无结局"，
 *     调用方（Composer）无从知道消息到底送出去没有；配合它在提交瞬间 `setValue('')`，
 *     失败即丢文本（AC1/AC3）。
 *  ② 发送路径只读 409 的 `message`（`readErrorDetail`），把 #596 特意结构化的
 *     `pending_decisions` 在读出的那一刻丢掉 ⇒ 裁决面板不会出现，用户只拿到一句
 *     照着做不了的协议原文（AC2）。
 *
 *  手法与 `useSession.recover.test.tsx` 同源：mock api 层，hook 本体/分派表/投影
 *  全走真实代码路径。
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
  };
});

import { getSessionEvents, sendMessage, type PendingDecision } from '../lib/api';
import { EARLY_RESPONSE_WINDOW_MS, useSession } from './useSession';

const SID = 's-1';

const PENDING: PendingDecision[] = [
  {
    tool_call_id: 'call_2',
    tool_name: 'bash',
    state: 'UNKNOWN',
    default_action: 'DEFER',
    risk_level: 'high',
    probe: { verifiable: false, suggested_action: null },
  },
];

/** 后端 `http_error` 对 RecoveryConflict 的结构化 detail（后端已带 pending_decisions）。 */
const conflictResponse = (): Response =>
  new Response(
    JSON.stringify({
      detail: {
        message:
          '存在需要人工裁决的 UNKNOWN Operation（bash）：先 POST /api/sessions/s-1/recover 携带 decisions=[{tool_call_id, verdict}] 结清',
        pending_decisions: PENDING,
      },
    }),
    { status: 409, headers: { 'content-type': 'application/json' } },
  );

/** 只有一个 dangling tool_call 的会话 ⇒ `isRecoverableRun` 为真（真机场景同形）。 */
const danglingHistory = (sid: string): AgentEvent[] =>
  [
    { type: 'session/started', seq: 1, session_id: sid, data: {}, run_id: null, step_id: null },
    { type: 'run/started', seq: 2, session_id: sid, data: {}, run_id: 'r-1', step_id: null },
    {
      type: 'tool/call', seq: 3, session_id: sid, run_id: 'r-1', step_id: 1,
      data: { tool_call_id: 'call_2', name: 'bash', args: {} },
    },
  ] as AgentEvent[];

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
}

/** 进到 viewing(SID)（发送路径的前置：用户正在看这个待裁决会话）。 */
async function enterViewing(): Promise<void> {
  await act(async () => { captured.hook!.selectSession(SID); });
  await act(async () => {});
  expect(captured.hook!.conversation?.session_id).toBe(SID);
}

/** 发一条并等它落定；返回 `sendMessage` 的结局（`true` = 已受理）。 */
async function send(content: string): Promise<boolean | void> {
  let outcome: boolean | void = undefined;
  await act(async () => {
    outcome = await captured.hook!.sendMessage(SID, content);
  });
  return outcome;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  vi.useFakeTimers();
  vi.mocked(sendMessage).mockReset();
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

describe('#1029 AC1/AC2 — 发送撞待裁决 409：结局如实 + 清单交给面板', () => {
  it('409 带 pending_decisions → 结局为"未受理"，并把清单落进 recoverState', async () => {
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    vi.mocked(sendMessage).mockResolvedValue(conflictResponse());
    await mount();
    await enterViewing();

    const accepted = await send('这句话必须留住');

    // ① 结局如实回报：Composer 靠它决定"不清空"（AC1/AC3）。
    expect(accepted).toBe(false);
    // ② 清单进了裁决面板的唯一真相槽（不变量 #22：只来自后端 409 载荷）。
    const st = captured.hook!.recoverState;
    expect(st.pendingDecisions).toEqual(PENDING);
    expect(st.conflict).toBe(true);
    // ③ 横幅是人话，不是那段照着做不了的协议原文。
    expect(st.message).toContain('裁决');
    expect(st.message).not.toContain('decisions=[{tool_call_id, verdict}]');
  });

  it('裁决报告必须活过"发送失败自迁移"（viewing→live→viewing 后仍在）', async () => {
    // 这是 AC2 最脆的一环：发送失败会把 mode 从 live 迁回 viewing，而历史装载 effect
    // 原先"任何 mode 迁移都重置 recover 三态"——刚发布的清单会在同一批次被抹掉，
    // 面板永远不出现。
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    vi.mocked(sendMessage).mockResolvedValue(conflictResponse());
    await mount();
    await enterViewing();

    await send('留住的文本');

    // 自迁移已经发生（失败收口 setMode(viewing)）⇒ 报告仍在。
    expect(captured.hook!.recoverState.pendingDecisions).toEqual(PENDING);
    await act(async () => {});
    expect(captured.hook!.recoverState.pendingDecisions).toEqual(PENDING);
  });

  it('切到别的会话才清裁决报告（结论不跨会话存活）', async () => {
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    vi.mocked(sendMessage).mockResolvedValue(conflictResponse());
    await mount();
    await enterViewing();
    await send('留住的文本');
    expect(captured.hook!.recoverState.pendingDecisions).toEqual(PENDING);

    await act(async () => { captured.hook!.selectSession('s-2'); });
    await act(async () => {});

    expect(captured.hook!.recoverState.pendingDecisions).toBeNull();
  });

  it('被受理的发送 → 结局为 true（Composer 据此清空）', async () => {
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    vi.mocked(sendMessage).mockResolvedValue(
      new Response(JSON.stringify({ status: 'queued', queue_id: 'q-1' }), {
        status: 200, headers: { 'content-type': 'application/json' },
      }),
    );
    await mount();
    await enterViewing();

    expect(await send('正常受理')).toBe(true);
    // 受理路径不发布裁决报告（不误报）。
    expect(captured.hook!.recoverState.pendingDecisions).toBeNull();
  });

  it('普通失败（非裁决 409）→ 结局为"未受理"，但不发布裁决报告', async () => {
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    vi.mocked(sendMessage).mockResolvedValue(
      new Response(JSON.stringify({ detail: 'boom' }), {
        status: 500, headers: { 'content-type': 'application/json' },
      }),
    );
    await mount();
    await enterViewing();

    expect(await send('服务端炸了')).toBe(false);
    expect(captured.hook!.recoverState.pendingDecisions).toBeNull();
    expect(captured.hook!.error).toContain('续聊失败');
  });

  it('steer 打空 409 仍走改投 queue（不误判成待裁决）', async () => {
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    // 第一次 steer → 409 打空标记；第二次 queue → 受理。
    vi.mocked(sendMessage)
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ detail: "steer requires an active run; use mode='queue' to enqueue" }),
          { status: 409, headers: { 'content-type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: 'queued', queue_id: 'q-2' }), {
          status: 200, headers: { 'content-type': 'application/json' },
        }),
      );
    await mount();
    await enterViewing();

    let accepted: boolean | void = undefined;
    await act(async () => {
      accepted = await captured.hook!.sendSteer(SID, '改投成功');
    });

    expect(accepted).toBe(true);
    expect(vi.mocked(sendMessage).mock.calls[0][1]).toMatchObject({ mode: 'steer' });
    expect(vi.mocked(sendMessage).mock.calls[1][1]).toMatchObject({ mode: 'queue' });
    expect(captured.hook!.recoverState.pendingDecisions).toBeNull();
  });

  it('票面原始序列：steer 打空 409 → queue 重投撞裁决 409 → 未受理且清单在（AC1 的"两次 409"）', async () => {
    // issue #1029「复现步骤」的字面路径：Ctrl+Enter 先 steer（409 打空——待裁决会话
    // 没有活跃 run），前端以 queue 重投 → **再 409**，这一次才是待裁决闸门。
    // 两路皆拒是这条票的现场，必须端到端成立：**第二次**那份 409 同样要把清单交出来，
    // 否则用户在最关键的第二次失败上又失去引导（回退链见 sendFollowUp 的 retry-queue 支）。
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    vi.mocked(sendMessage)
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ detail: "steer requires an active run; use mode='queue' to enqueue" }),
          { status: 409, headers: { 'content-type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(conflictResponse());
    await mount();
    await enterViewing();

    let accepted: boolean | void = undefined;
    await act(async () => {
      accepted = await captured.hook!.sendSteer(SID, '待裁决会话里的一条消息');
    });

    expect(accepted).toBe(false);
    expect(vi.mocked(sendMessage).mock.calls).toHaveLength(2);
    expect(vi.mocked(sendMessage).mock.calls[0][1]).toMatchObject({ mode: 'steer' });
    expect(vi.mocked(sendMessage).mock.calls[1][1]).toMatchObject({ mode: 'queue' });
    expect(captured.hook!.recoverState.pendingDecisions).toEqual(PENDING);
    expect(captured.hook!.error).toContain('裁决');
  });

  /* AC2 后半（「完成裁决后该文本可一键重发」）**刻意不在这里加用例**：
   * 我写过一版「409 未受理 → submitDecisions → 同一段文本重发被受理」的闭环用例，
   * 但把它跑在**修复前**的实现（fe0e8847）上**照样全绿** —— 因为数据层原本就
   * 如实回报 false、也原本就把 pendingDecisions 落进 recoverState（那两条由上面
   * L125/L234 两例覆盖）。#1029 的缺陷在**清空与呈现层**（Composer 无条件清空、
   * 横幅没有「去裁决」入口），而"裁决后重发"的成立条件是**文本还在输入框里** ——
   * 那由 `Composer.draftRetention.test.tsx` 的 AC3 组与 P2 组锁住（它们在修复前
   * 确实是红的）。在这里再写一遍只会得到一个**不会因本票缺陷变红**的装饰性用例。 */
});

/** #1029 P2（独立审查 Correctness P2-1）：**窗外**落定的失败同样不许吞掉文本。
 *
 *  缺陷形态：判定窗口（1200ms）内响应头没到 ⇒ 判为"正常流式"、先按已受理返回 `true`
 *  ⇒ Composer 清空草稿；随后那条响应其实**是 409 失败**，原实现只 `setError` 设了
 *  一条横幅就咽下结局。用户看到的是"横幅说失败、输入框却空了"——正是本票要消灭的
 *  丢文本，只是换到了更慢的网络上。
 *
 *  为什么这条路径真实存在（不是理论构造）：待裁决 409 在 `service.py:5061` 之前要先
 *  把 pending 裁决查出来（`resume_and_launch` → `recover`），冷启动/慢盘越过 1200ms
 *  完全正常；而窗内判据只量"响应头到没到"，量不到这个。更极端的是部署交付层攒包：
 *  实测 `POST /api/sessions` 的响应头 **44.158s** 才到（`lib/wsStream.ts` 的实测表），
 *  那一档下**每一次**失败都落在窗外。
 *
 *  成熟产品同款（本票的方案依据）：DSH 对分离投递（detached）的失败专门走
 *  `settleDetachedFailure`（packages/client/ui-conversation/src/client/input/facade.ts:765-775）
 *  ——恢复草稿，并把失败经**同一条** sink-settled 通道交回，而不是另开一条只显示不
 *  恢复的旁路；oh-my-pi `restoreDetachedDraft`（.../slash-commands/helpers/draft.ts:16-30）
 *  同义。这里同样把结局经 `failedDraft` 交回 Composer。
 */
describe('#1029 P2 — 窗外落定的失败：草稿必须回到用户手里', () => {
  /** 一个"响应头晚到"的响应：promise 由测试决定何时 resolve（越过判定窗口）。 */
  function lateResponse(): { promise: Promise<Response>; settle: (r: Response) => void } {
    let settle!: (r: Response) => void;
    const promise = new Promise<Response>((resolve) => { settle = resolve; });
    return { promise, settle };
  }

  /** 推过判定窗口（1200ms）：此后 sendFollowUp 已按"已受理"返回，不再等这条响应。
   *  必须显式推进——本文件用 fake timers，窗口不会自己走。
   *
   *  **顺序是承重的**（与 `useSession.liveness.test.tsx` 的 `driveLaunched` 同款）：
   *  先起请求、再推进计时器、最后才 await。反过来先 `await action()` 会死锁——那条
   *  promise 要等窗口计时器落定，而计时器只在我们 await 之后才被推进。 */
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

  it('窗外落定的 409 → failedDraft 拿到原文（不是只设一条横幅）', async () => {
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    const late = lateResponse();
    vi.mocked(sendMessage).mockReturnValue(late.promise);
    await mount();
    await enterViewing();

    // 发出后推进到窗口之外：此刻 sendFollowUp 已按"已受理"返回（无法 await 结局）。
    const accepted = await driveOutOfWindow(
      () => captured.hook!.sendMessage(SID, '窗外失败也必须留住'),
    );
    expect(accepted).toBe(true);
    // 还没落定 ⇒ 还没有失败回执（不能预先报错）。
    expect(captured.hook!.failedDraft).toBeNull();

    // 迟到落定：其实是 409 待裁决。
    await act(async () => { late.settle(conflictResponse()); });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft?.text).toBe('窗外失败也必须留住');
    // 同时仍是人话横幅 + 清单进面板（两条通道各司其职，AC1/AC2 不变）。
    expect(captured.hook!.error).toContain('裁决');
    expect(captured.hook!.recoverState.pendingDecisions).toEqual(PENDING);
  });

  it('窗外被拒绝（网络级失败）→ 同样交出原文', async () => {
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    let reject!: (e: Error) => void;
    const promise = new Promise<Response>((_res, rej) => { reject = rej; });
    vi.mocked(sendMessage).mockReturnValue(promise);
    await mount();
    await enterViewing();

    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '断网也要留住'));
    await act(async () => { reject(new Error('network down')); });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft?.text).toBe('断网也要留住');
    expect(captured.hook!.error).toContain('续聊失败');
  });

  it('同一段文本连续失败两次 → 每次都是新的回执对象（否则第二次不回填）', async () => {
    // 裸 string 会被 React 的 Object.is 挡掉：状态相等 ⇒ 不重渲染 ⇒ effect 不触发。
    // "重发一次、又失败一次"正是这条路径的常态，必须各算一次注入。
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    const first = lateResponse();
    vi.mocked(sendMessage).mockReturnValueOnce(first.promise);
    await mount();
    await enterViewing();

    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '同一段'));
    await act(async () => { first.settle(conflictResponse()); });
    await act(async () => { await Promise.resolve(); });
    const firstReceipt = captured.hook!.failedDraft;
    expect(firstReceipt?.text).toBe('同一段');

    const second = lateResponse();
    vi.mocked(sendMessage).mockReturnValueOnce(second.promise);
    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '同一段'));
    await act(async () => { second.settle(conflictResponse()); });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft?.text).toBe('同一段');
    expect(captured.hook!.failedDraft).not.toBe(firstReceipt);
  });

  it('窗外落定的是**成功**（queued 确认）→ 不产生失败回执（不误报）', async () => {
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    const late = lateResponse();
    vi.mocked(sendMessage).mockReturnValue(late.promise);
    await mount();
    await enterViewing();

    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '其实成功了'));
    await act(async () => {
      late.settle(new Response(JSON.stringify({ status: 'queued', queue_id: 'q-9' }), {
        status: 200, headers: { 'content-type': 'application/json' },
      }));
    });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft).toBeNull();
  });

  it('用户在迟到落定前切走会话 → 不把旧会话的原文倒进新会话（代际闸）', async () => {
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    const late = lateResponse();
    vi.mocked(sendMessage).mockReturnValue(late.promise);
    await mount();
    await enterViewing();

    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '属于旧会话的文本'));
    await act(async () => { captured.hook!.selectSession('s-2'); });
    await act(async () => {});
    await act(async () => { late.settle(conflictResponse()); });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft).toBeNull();
  });

  it('下一次投递清掉旧回执 → Composer 重新挂载（切页签）不会复活已重发的原文', async () => {
    // 缺陷形态：Composer 挂在 `tab.key === 'chat'` 分支下，切页签会卸载它。
    // 若失败回执留在 hook 里不清，回到对话页签时 effect 重新挂载、把上一轮的原文
    // 又注入一次——而那条消息多半早已重发成功，用户看到"凭空冒出来的草稿"，
    // 再按 Enter 就是重复投递。
    vi.mocked(getSessionEvents).mockResolvedValue(danglingHistory(SID));
    const late = lateResponse();
    vi.mocked(sendMessage).mockReturnValueOnce(late.promise);
    await mount();
    await enterViewing();

    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '失败过一次的原文'));
    await act(async () => { late.settle(conflictResponse()); });
    await act(async () => { await Promise.resolve(); });
    expect(captured.hook!.failedDraft?.text).toBe('失败过一次的原文');

    // 用户重发（这一次成功）：回执必须随之作废。
    const ok = lateResponse();
    vi.mocked(sendMessage).mockReturnValueOnce(ok.promise);
    await driveOutOfWindow(() => captured.hook!.sendMessage(SID, '失败过一次的原文'));
    await act(async () => {
      ok.settle(new Response(JSON.stringify({ status: 'queued', queue_id: 'q-1' }), {
        status: 200, headers: { 'content-type': 'application/json' },
      }));
    });
    await act(async () => { await Promise.resolve(); });

    expect(captured.hook!.failedDraft).toBeNull();
  });
});
