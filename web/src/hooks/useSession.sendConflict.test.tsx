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
import { useSession } from './useSession';

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
});
