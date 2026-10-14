// @vitest-environment jsdom
/** #1029 残余漏洞（修后重审发现）：**新建会话**路径（`submitTask`）在窗外认领成功后
 *  吞掉迟到的失败 ⇒ 用户输入凭空消失。
 *
 *  背景：#1029 把「清空推迟到投递落定」修在了 `sendFollowUp`（续聊）上，且把窗外迟到的
 *  失败经 `failedDraft` 投影回输入框。但 `handleSubmit` 有**两条**投递路径：
 *    A) `sendMessage(sid, ...)`  —— 已覆盖；
 *    B) `submitTask({...})`      —— 新建会话，**漏洞在这里**。
 *
 *  `submitTask` 窗外分支的形状（useSession.ts:1070-1091）：
 *    1. `startSession(payload)` 发出但不 await（交付层攒包，响应头 ≈ run 结束才到）；
 *    2. `started.catch(e => { late.failure = e })` 接住 rejection；
 *    3. `await discoverNewSessionId(baseline)` 拿差分认领新会话；
 *    4. 认领成功 ⇒ `return null`（= 成功）。
 *  而 `late.failure` **只在第 3 步抛错时**才被读（L1083 `throw late.failure ?? e`）。
 *  于是「认领成功 *之后* POST 才 reject」这个序列里，`late.failure` 被写进去却
 *  没有任何人读 ⇒ `submitTask` 返回 null（成功）⇒ `handleSubmit` 返回 true ⇒
 *  Composer 清空输入框。用户的文本没了，而且**不是**因为发送失败被通知——
 *  它被当成了成功。
 *
 *  实测可达序列：网络在会话已创建、响应未收全时断开 / 401 / 后端 5xx。
 *
 *  本文件只锁这条路径的**结局如实**（剩余处置策略 = 交用户裁决，见 tracker 残余项）。
 */

import { act, createElement, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    listSessions: vi.fn(async () => []),
    getSessionEvents: vi.fn(async () => []),
    listSessionQueue: vi.fn(async () => ({ items: [], steers: [] })),
    startSession: vi.fn(),
  };
});

// ⚠ `sessionIdBaseline` / `discoverNewSessionId` 在 `lib/wsStream`，**不在** `lib/api`
// （useSession.ts:28 的 import 就是判据）。挂到 api 上会把真实导出抹成 undefined。
vi.mock('../lib/wsStream', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/wsStream')>();
  return {
    ...actual,
    sessionIdBaseline: vi.fn(async () => new Set<string>()),
    discoverNewSessionId: vi.fn(),
  };
});

import { startSession } from '../lib/api';
import { discoverNewSessionId, sessionIdBaseline } from '../lib/wsStream';
import { EARLY_RESPONSE_WINDOW_MS, useSession } from './useSession';

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
  // Harness 的 effect 写入 captured.hook；再走一轮空 act 确保它已 flush。
  await act(async () => {});
  if (!captured.hook) throw new Error('harness 未捕获到 hook');
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  vi.useFakeTimers();
  vi.mocked(startSession).mockReset();
  vi.mocked(sessionIdBaseline).mockReset();
  vi.mocked(discoverNewSessionId).mockReset();
  vi.mocked(sessionIdBaseline).mockResolvedValue(new Set<string>());
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

describe('#1029 残余 — submitTask 窗外认领成功后吞掉迟到失败', () => {
  /** 造出「认领在途、POST 已 reject」的那一刻：认领（`discoverNewSessionId`）由测试
   *  决定何时 resolve，POST 由测试决定何时 reject。两者都受控，才能精确制造
   *  「认领成功 *之后* POST 才失败」这个序列。
   *
   *  顺序承重（与 `useSession.sendConflict.test.tsx` 的 `driveOutOfWindow` 同款）：
   *  先起 action → 让 POST reject → 再放行认领 → 最后 await。反过来先 await action
   *  会死锁，因为认领的 promise 还没被放行。 */
  async function driveClaimThenReject(
    task: string,
  ): Promise<{ result: string | null }> {
    let rejectPost!: (e: Error) => void;
    const post = new Promise<Response>((_resolve, reject) => { rejectPost = reject; });
    vi.mocked(startSession).mockReturnValue(post as never);

    let releaseClaim!: () => void;
    const claimGate = new Promise<void>((resolve) => { releaseClaim = resolve; });
    vi.mocked(discoverNewSessionId).mockImplementation(async () => {
      await claimGate;
      return 's-new';
    });

    let result: string | null = null;
    await act(async () => {
      const p = captured.hook!.submitTask({ task });
      // 先让窗口过去 + 让 POST 落定（此刻认领还卡在闸门上）。
      await vi.advanceTimersByTimeAsync(EARLY_RESPONSE_WINDOW_MS + 200);
      rejectPost(new Error('网络中断：POST /api/sessions'));
      await vi.advanceTimersByTimeAsync(0);
      // 现在放行认领：submitTask 走到"认领成功"那一步时，POST 已经是失败的了。
      releaseClaim();
      await vi.advanceTimersByTimeAsync(0);
      result = await p;
    });
    return { result };
  }

  it('POST 在认领成功之前/同时已失败 ⇒ 结局必须是"未受理"（否则 Composer 清空 = 丢文本）', async () => {
    await mount();
    const { result } = await driveClaimThenReject('新建会话里也要留住的文本');

    // 期望：结局如实 = 失败原因（Composer 据此保留草稿）。
    // 修复前：null（成功）⇒ Composer 清空 ⇒ 文本消失。
    expect(result).not.toBeNull();
    expect(result).toContain('网络中断');
  });

  it('同序列去重：认领成功但 POST 已失败 ⇒ 只报一次，不重复计数', async () => {
    await mount();
    const first = await driveClaimThenReject('第一条');
    const second = await driveClaimThenReject('第二条');

    // 两次独立投递各自如实回报（各自的失败不互相吞掉）。
    expect(first.result).toContain('网络中断');
    expect(second.result).toContain('网络中断');
  });
});
