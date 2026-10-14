// @vitest-environment jsdom
/** #1029：待裁决会话发送（`POST /messages` → 409 RecoveryConflict）的**结局分派**。
 *
 *  缺陷形态（票面根因②）：`decideFollowUpOutcome` 的 409 分支只认 steer 打空标记，
 *  其余 409 一律落泛化 fail；而 `settle` 用 `readErrorDetail` 只取 `detail.message`
 *  ⇒ 后端已结构化的 `pending_decisions`（#596 交付）被前端发送路径丢掉，
 *  用户既拿不到人话文案、也走不到 `RecoveryDecisionPanel`。
 *
 *  本文件锁三件事：
 *   1. 结构化的 409 → 结局携带后端清单（不是字符串匹配出来的）；
 *   2. `recoverState` 被点亮成 conflict + pendingDecisions ⇒ App 据此渲染裁决面板
 *      （#22：清单只来自后端载荷，本 hook 不合成、不缓存）；
 *   3. 失败时 `sendMessage` resolve **false** —— 这是 composer「保留用户输入」的
 *      唯一信号（`11 §6.1`：「409 冲突后 MUST 刷新权威状态并保留用户未发送的输入」）。
 *
 *  手法同 `useSession.recover.test.tsx`：只 mock 传输层（lib/api），
 *  useSession 本体 / 分派表 / 投影全走真实代码路径。
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
    sendMessage: vi.fn(),
  };
});

import { sendMessage as apiSendMessage, type PendingDecision } from '../lib/api';
import { useSession } from './useSession';

const SID = 's-1029';

/** 已收口会话历史（不开实时流，聚焦发送路径本身）。 */
const idleHistory = (sid: string): AgentEvent[] =>
  [
    { type: 'session/started', seq: 1, session_id: sid, data: {}, run_id: null, step_id: null },
    { type: 'run/completed', seq: 2, session_id: sid, data: {}, run_id: 'r-1', step_id: null },
  ] as AgentEvent[];

/** 后端 409 的结构化 detail（`domain_errors.http_error` 的形状，逐字段对齐）。 */
const PENDING: PendingDecision[] = [
  {
    tool_call_id: 'call_82ade',
    tool_name: 'bash',
    state: 'UNKNOWN',
    default_action: 'DEFER',
    risk_level: 'high',
    probe: { verifiable: false, suggested_action: null },
  },
];

/** 后端协议原文（`session/service.py::recover` 的 RecoveryConflict message）——
 *  刻意用真实形状：本票要证明前端**不再**把它当最终文案透传。 */
const PROTOCOL_MESSAGE =
  '存在需要人工裁决的 UNKNOWN Operation（bash(tool_call_id=call_82ade)）：先 POST ' +
  '/api/sessions/{id}/recover 携带 decisions=[{tool_call_id, verdict}] 结清（verdict ∈ ' +
  'CONFIRM_SUCCESS / CONFIRM_FAILURE / RETRY / ABANDON / DEFER）；裁决前请先核查该调用的外部事实';

const recoveryConflict409 = (): Response =>
  new Response(JSON.stringify({ detail: { message: PROTOCOL_MESSAGE, pending_decisions: PENDING } }), {
    status: 409,
    headers: { 'content-type': 'application/json' },
  });

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
  vi.mocked(apiSendMessage).mockReset();
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  container?.remove();
  root = null;
  container = null;
  captured.hook = null;
  vi.clearAllMocks();
});

describe('#1029 发送路径 · 待裁决 409 的结局分派', () => {
  it('结构化 409：结局携带后端 pending_decisions，点亮 recoverState 供 App 渲染裁决面板', async () => {
    vi.mocked(apiSendMessage).mockResolvedValue(recoveryConflict409());
    await mount();
    await enterViewing(SID);

    let accepted: boolean | undefined;
    await act(async () => {
      accepted = await captured.hook!.sendMessage(SID, '这条文本不能丢');
    });

    const st = captured.hook!.recoverState;
    expect(st.conflict).toBe(true);
    expect(st.pendingDecisions).toEqual(PENDING);
    // 清单只来自后端载荷（#22）：本 hook 不合成、不缓存
    expect(vi.mocked(apiSendMessage)).toHaveBeenCalledTimes(1);
    expect(accepted).toBe(false);
  });

  it('文案是人话（可行动），不透传后端协议原文', async () => {
    vi.mocked(apiSendMessage).mockResolvedValue(recoveryConflict409());
    await mount();
    await enterViewing(SID);

    await act(async () => {
      await captured.hook!.sendMessage(SID, '这条文本不能丢');
    });

    const error = captured.hook!.error ?? '';
    expect(error).toContain('待人工裁决');
    expect(error).toContain('裁决面板');
    // 协议原文（POST /recover、tool_call_id、verdict 词表）不进用户层横幅
    expect(error).not.toContain('POST /api/sessions');
    expect(error).not.toContain('tool_call_id=');
    expect(error).not.toContain('CONFIRM_SUCCESS');
  });

  it('非裁决的 409（无 pending_decisions）：保持既有泛化失败，不误报成待裁决', async () => {
    vi.mocked(apiSendMessage).mockResolvedValue(
      new Response(JSON.stringify({ detail: '存在需要人工裁决的高风险操作' }), {
        status: 409,
        headers: { 'content-type': 'application/json' },
      }),
    );
    await mount();
    await enterViewing(SID);

    let accepted: boolean | undefined;
    await act(async () => {
      accepted = await captured.hook!.sendMessage(SID, '这条文本不能丢');
    });

    const st = captured.hook!.recoverState;
    expect(st.conflict).toBe(false);
    expect(st.pendingDecisions).toBeNull();
    expect(accepted).toBe(false);
    // 纯字符串 detail 没有可消费的清单 ⇒ 仍原样转述（不代后端编话）
    expect(captured.hook!.error).toContain('存在需要人工裁决的高风险操作');
  });

  it('受理成功（JSON 收据）：resolve true —— composer 据此才允许清空', async () => {
    vi.mocked(apiSendMessage).mockResolvedValue(
      new Response(JSON.stringify({ status: 'queued', queue_id: 'q-1' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    await mount();
    await enterViewing(SID);

    let accepted: boolean | undefined;
    await act(async () => {
      accepted = await captured.hook!.sendMessage(SID, '这条文本不能丢');
    });

    expect(accepted).toBe(true);
    expect(captured.hook!.recoverState.conflict).toBe(false);
  });

  it('网络失败（无响应）：resolve false + 可读失败态', async () => {
    vi.mocked(apiSendMessage).mockRejectedValue(new Error('Failed to fetch'));
    await mount();
    await enterViewing(SID);

    let accepted: boolean | undefined;
    await act(async () => {
      accepted = await captured.hook!.sendMessage(SID, '这条文本不能丢');
    });

    expect(accepted).toBe(false);
    expect(captured.hook!.error).toContain('续聊失败');
  });
});
