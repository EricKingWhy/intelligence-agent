// @vitest-environment jsdom
/** #1029 残余漏洞（修后重审发现）：**新建会话**路径（`submitTask`）在窗外认领成功后
 *  吞掉迟到的失败 ⇒ 用户输入凭空消失。
 *
 *  背景：#1029 把「清空推迟到投递落定」修在了 `sendFollowUp`（续聊）上，且把窗外迟到的
 *  失败经 `failedDraft` 投影回输入框。但 `handleSubmit` 有**两条**投递路径：
 *    A) `sendMessage(sid, ...)`  —— 已覆盖；
 *    B) `submitTask({...})`      —— 新建会话，**漏洞在这里**。
 *
 *  `submitTask` 窗外分支的形状：
 *    1. `startSession(payload)` 发出但不 await（交付层攒包，响应头 ≈ run 结束才到）；
 *    2. `started.catch(e => { late.failure = e })` 接住 rejection；
 *    3. `await discoverNewSessionId(baseline)` 拿差分认领新会话；
 *    4. 认领成功 ⇒ `return null`（= 成功）。
 *  而 `late.failure` **只在第 3 步抛错时**才被读（认领 catch 里的
 *  `throw late.failure ?? e`）。于是「认领成功 *之后* POST 才 reject」这个序列里，
 *  `late.failure` 被写进去却没有任何人读 ⇒ `submitTask` 返回 null（成功）⇒
 *  `handleSubmit` 返回 true ⇒ Composer 清空输入框。用户的文本没了，而且**不是**
 *  因为发送失败被通知——它被当成了成功。
 *
 *  实测可达序列：网络在会话已创建、响应未收全时断开 / 401 / 后端 5xx。
 *
 *  ⚠ 本文件刻意**不写 `useSession.ts` 的行号区间**：写死过（"1070-1091"/"L1083"），
 *  随后几笔插入就让它们指向无关代码——独立审查连续两轮抓到同一类"可证伪断言
 *  不成立"。指代一律用标识符与函数名。
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
// （`useSession.ts` 顶部那句 import 就是判据，别写行号）。挂到 api 上会把真实导出抹成 undefined。
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
  /** 造出「认领成功、随后 POST 才 reject」的序列，且 reject 相对认领的**微任务偏移**
   *  可调（`offset` ≥ 0 表示 reject 比认领晚 offset 个微任务）。
   *
   *  ⚠ 这个 offset 维度是承重的，不是花活：第一版修复用「读一次 `late.failure` 快照」
   *  判定，只在 offset ≤ 0 时命中（独立审查 Correctness 轴 F1 的发现，我用扫档复现：
   *  offset=-2/-1/0 → CAUGHT，offset=+1..+4 → MISS）。只测 offset=0 的用例会给人
   *  "已守住"的错觉，所以这里显式按 offset 驱动并逐个断言。
   *
   *  顺序承重（与 `useSession.sendConflict.test.tsx` 的 `driveOutOfWindow` 同款）：
   *  先起 action → 推过窗口 → 放行认领 → 按 offset 插微任务 → reject → 最后 await。 */
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
        // 认领已放行；按 offset 决定 reject 落在认领之后第几个微任务。
        for (let i = 0; i < offset; i++) await Promise.resolve();
        rejectPost(new Error('网络中断：POST /api/sessions'));
      }
      await vi.advanceTimersByTimeAsync(0);
      result = await p;
    });
    return { result, postUnsettled };
  }

  it('POST 在认领当刻已失败（offset=0）⇒ 草稿必须回到用户手里', async () => {
    await mount();
    const { result } = await driveClaimThenReject('新建会话里也要留住的文本', 0, true);

    // Composer 保持草稿有**两条**合法出路（见修复注释）：
    //   ① 返回失败原因 ⇒ handleSubmit 给 false ⇒ Composer 不清空；
    //   ② 返回 null（已接管）但把失败经 failedDraft 交回 ⇒ Composer 回填。
    // 不论走哪条，用户都必须拿到原文。
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

      // 这是 F1 的正面锁：offset ≥ 1 时快照判定会返回 null 且不发任何回执，
      // 于是 result === null 且 failedDraft === null ⇒ 这就是静默丢文本。
      const recovered = result !== null || captured.hook!.failedDraft !== null;
      expect(recovered, `offset=${offset} 必须有一条失败出路，实测 result=${String(result)} / failedDraft=${JSON.stringify(captured.hook!.failedDraft)}`).toBe(true);
      // 且回执里的原文与提交内容一致（不能回填成别的会话/别的文本）。
      const text = result !== null ? result : captured.hook!.failedDraft!.text;
      expect(text).toContain(`offset=${offset}`);
      await act(async () => { root!.unmount(); });
      container?.remove(); root = null; container = null; captured.hook = null;
    }
  }, 30000);

  it('反证：POST 仍在途（未 reject）时认领成功 ⇒ 仍按"已接管"返回 null，不误报失败', async () => {
    // 这是守卫最容易过头的方向：正常流式的响应头要等 run 结束才到，认领成功时
    // POST 必然还悬着。若把"还悬着"也当失败，用户每次新建会话都会被误报成失败。
    await mount();
    const { result, postUnsettled } = await driveClaimThenReject('正常新建', 0, false, false);

    expect(postUnsettled).toBe(true);   // 前提：POST 确实没落定
    expect(result).toBeNull();          // 视为已接管
    expect(captured.hook!.failedDraft).toBeNull(); // 且不得发失败回执
  });

  it('ownError 提交（TaskCreationDialog）迟到失败 ⇒ **不得**把弹窗文本灌进 Composer', async () => {
    // App.handleStartTaskInProject（App.tsx:774-784）用 ownError 声明"失败由我
    // 在浮层里就地显示"。那种提交的原文在**弹窗的输入框**里，不在 Composer；
    // 若照样 reportDetachedFailure，用户会在对话输入框里看到一段凭空冒出来的
    // 任务文本（他没在那儿打过字）——那是本票要消灭的"输入框内容不可预期"。
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
