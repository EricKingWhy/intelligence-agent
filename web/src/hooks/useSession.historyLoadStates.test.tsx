// @vitest-environment jsdom
/** #1521（FE-05/FE-06）数据层契约：区分「首次加载失败」「真实空态」「显式点击 404」。
 *
 *  症状红证（票面「实施前按当前树重核症状」——本文件每条红测试即一条症状证明）：
 *   - FE-05：首次加载 500 只落到单槽 `error` 横幅，Conversation 侧把 `conversation`
 *     为 null 渲染成「暂无对话」空态 ⇒ 矛盾组合 + 无就地重试。修后：失败经
 *     `historyLoadError` 专槽上行（面板就地重试），横幅通道不再同时置位。
 *   - FE-06：显式点行 404 与启动恢复 404 共用同一条安静复位——前者毫无反馈且
 *     过期行留在列表里。修后：`selectSession` 显式路径给温和提示并刷新列表；
 *     启动恢复（不经 selectSession）保持安静复位、不循环报错。
 *
 *  手法同 `useSession.detachedFailure.test.tsx`：只 mock 传输层，hook 本体真实。
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
    sendMessage: vi.fn(),
    startSession: vi.fn(),
  };
});

// ⚠ `sessionIdBaselineDetailed` / `discoverNewSessionId` 在 `lib/wsStream`，不在
// `lib/api`（同 detachedFailure 测试的注）：认领基线读不到即走降级路径，
// 本文件不考时钟锚点。测试⑨的 submitTask 要走真实认领入口，必须有这个 mock
// 兜住，否则测试里会发起真实 fetch。
vi.mock('../lib/wsStream', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/wsStream')>();
  return {
    ...actual,
    sessionIdBaselineDetailed: vi.fn(async () => ({ ids: new Set<string>(), serverNowMs: null })),
    discoverNewSessionId: vi.fn(),
  };
});

import {
  NotFoundError,
  listSessions,
  getSessionEvents,
  startSession,
} from '../lib/api';
import { SELECTED_SESSION_KEY } from '../lib/sessionRestore';
import { useSession } from './useSession';

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

const SID = 's-1521';
const OTHER = 's-other';

const idleHistory = (sid: string) =>
  [
    { type: 'session/started', seq: 1, session_id: sid, data: {}, run_id: null, step_id: null },
    { type: 'user/message', seq: 2, session_id: sid, data: { content: '帮我看看这条历史' }, run_id: null, step_id: null },
    { type: 'run/completed', seq: 3, session_id: sid, data: {}, run_id: 'r-1', step_id: null },
  ] as never[];

const boom500 = () => new Error('模拟服务端 500');

beforeEach(() => {
  vi.mocked(listSessions).mockClear();
  vi.mocked(getSessionEvents).mockReset();
  vi.mocked(getSessionEvents).mockResolvedValue([]);
  localStorage.clear();
});

afterEach(() => {
  act(() => { root?.unmount(); });
  container?.remove();
  container = null;
  root = null;
  captured.hook = null;
});

describe('#1521 FE-05：首次加载失败 / 真实空态 / 后台重读失败三分', () => {
  it('首次加载 500 → historyLoadError 专槽在位（就地失败面），error 横幅通道不再同时置位', async () => {
    vi.mocked(getSessionEvents).mockRejectedValue(boom500());
    await mount();
    await act(async () => { captured.hook!.selectSession(SID); });
    await act(async () => {});

    expect(captured.hook!.historyLoadError).toEqual({
      sid: SID,
      message: expect.stringContaining('模拟服务端 500'),
    });
    // 矛盾组合的消除：失败面板是唯一表面，通用横幅不再重复同一句
    expect(captured.hook!.error).toBeNull();
    expect(captured.hook!.conversation).toBeNull();
  });

  it('真实空历史（200 + 0 事件）→ 不置失败面（空态照常渲染）', async () => {
    await mount();
    await act(async () => { captured.hook!.selectSession(SID); });
    await act(async () => {});

    expect(captured.hook!.historyLoadError).toBeNull();
    expect(captured.hook!.error).toBeNull();
    expect(captured.hook!.conversation?.session_id).toBe(SID);
    expect(captured.hook!.conversation!.turns).toHaveLength(0);
  });

  it('后台重读失败（同会话对账）→ 保留已有对话 + 横幅（既有契约），不置失败面', async () => {
    vi.mocked(getSessionEvents).mockResolvedValueOnce(idleHistory(SID));
    await mount();
    await act(async () => { captured.hook!.selectSession(SID); });
    await act(async () => {});
    expect(captured.hook!.conversation?.session_id).toBe(SID);
    expect(captured.hook!.conversation!.turns.length).toBeGreaterThan(0);

    vi.mocked(getSessionEvents).mockRejectedValue(boom500());
    await act(async () => { captured.hook!.reloadConversation(SID); });
    await act(async () => {});

    expect(captured.hook!.historyLoadError).toBeNull();
    expect(captured.hook!.error).toContain('模拟服务端 500');
    expect(captured.hook!.conversation?.session_id).toBe(SID);
    expect(captured.hook!.conversation!.turns.length).toBeGreaterThan(0);
  });

  it('重试可恢复：失败面在位时 reloadConversation 成功 → 失败面退场、对话就位', async () => {
    vi.mocked(getSessionEvents).mockRejectedValueOnce(boom500()).mockResolvedValueOnce(idleHistory(SID));
    await mount();
    await act(async () => { captured.hook!.selectSession(SID); });
    await act(async () => {});
    expect(captured.hook!.historyLoadError).not.toBeNull();

    await act(async () => { captured.hook!.reloadConversation(SID); });
    await act(async () => {});
    expect(captured.hook!.historyLoadError).toBeNull();
    expect(captured.hook!.conversation?.session_id).toBe(SID);
  });

  it('切会话不污染：A 加载失败后切 B 成功 → 失败面清空、内容属于 B', async () => {
    vi.mocked(getSessionEvents).mockImplementation(async (sid: string) => {
      if (sid === SID) throw boom500();
      return idleHistory(sid);
    });
    await mount();
    await act(async () => { captured.hook!.selectSession(SID); });
    await act(async () => {});
    expect(captured.hook!.historyLoadError?.sid).toBe(SID);

    await act(async () => { captured.hook!.selectSession(OTHER); });
    await act(async () => {});
    expect(captured.hook!.historyLoadError).toBeNull();
    expect(captured.hook!.conversation?.session_id).toBe(OTHER);
  });

  it('A 失败 → 切 B 也失败：失败面跟随当前会话（审查 B-F4：失败→失败顺序切换）', async () => {
    vi.mocked(getSessionEvents).mockImplementation(async (sid: string) => {
      throw new Error(`炸-${sid}`);
    });
    await mount();
    await act(async () => { captured.hook!.selectSession('s-a'); });
    await act(async () => {});
    expect(captured.hook!.historyLoadError).toEqual({
      sid: 's-a',
      message: expect.stringContaining('炸-s-a'),
    });

    await act(async () => { captured.hook!.selectSession('s-b'); });
    await act(async () => {});
    // 面板归属新失败：A 的原文不得残留
    expect(captured.hook!.historyLoadError).toEqual({
      sid: 's-b',
      message: expect.stringContaining('炸-s-b'),
    });
    expect(captured.hook!.conversation).toBeNull();
    expect(captured.hook!.error).toBeNull();
  });
});

describe('#1521 FE-06：显式点击 404 给温和提示并刷列表；启动恢复 404 保持安静复位', () => {
  it('显式 selectSession 命中 404 → missingSessionNotice 在位 + 列表刷新（过期行收敛）', async () => {
    vi.mocked(getSessionEvents).mockRejectedValue(new NotFoundError());
    await mount();
    const callsBefore = vi.mocked(listSessions).mock.calls.length;
    await act(async () => { captured.hook!.selectSession(SID); });
    await act(async () => {});

    expect(captured.hook!.missingSessionNotice).toEqual(expect.stringContaining('不存在'));
    expect(vi.mocked(listSessions).mock.calls.length).toBeGreaterThan(callsBefore);
    // 会话已不存在：不留在 viewing（安静复位的既有语义保留），stored id 清空
    expect(captured.hook!.selectedId).toBeNull();
    expect(localStorage.getItem(SELECTED_SESSION_KEY)).toBeNull();
  });

  it('启动恢复过期 id 404（不经 selectSession）→ 安静复位：无提示、不循环报错', async () => {
    localStorage.setItem(SELECTED_SESSION_KEY, SID);
    vi.mocked(getSessionEvents).mockRejectedValue(new NotFoundError());
    await mount();
    await act(async () => {});

    expect(captured.hook!.selectedId).toBeNull();
    expect(captured.hook!.missingSessionNotice).toBeNull();
    expect(localStorage.getItem(SELECTED_SESSION_KEY)).toBeNull();
  });

  it('提示可显式关闭；再次成功切换也会自然清掉', async () => {
    vi.mocked(getSessionEvents).mockRejectedValueOnce(new NotFoundError());
    await mount();
    await act(async () => { captured.hook!.selectSession(SID); });
    await act(async () => {});
    expect(captured.hook!.missingSessionNotice).not.toBeNull();

    await act(async () => { captured.hook!.dismissMissingSessionNotice(); });
    expect(captured.hook!.missingSessionNotice).toBeNull();

    // 切到正常会话：提示即便没手动关也会被清（不跨会话存活）
    await act(async () => { captured.hook!.selectSession(OTHER); });
    await act(async () => {});
    expect(captured.hook!.missingSessionNotice).toBeNull();
  });

  it('提示在场 → 提交新任务 → 提示清场（审查 A-F2：清场发生在 submitTask 入口，早于认领结果）', async () => {
    vi.mocked(getSessionEvents).mockRejectedValue(new NotFoundError());
    await mount();
    await act(async () => { captured.hook!.selectSession(SID); });
    await act(async () => {});
    expect(captured.hook!.missingSessionNotice).not.toBeNull();

    // 认领失败也无妨：入口清场不依赖认领结果
    vi.mocked(getSessionEvents).mockReset();
    vi.mocked(getSessionEvents).mockResolvedValue([]);
    vi.mocked(startSession).mockRejectedValue(new Error('网络中断：POST /api/sessions'));
    await act(async () => { await captured.hook!.submitTask({ task: '新任务' }); });
    expect(captured.hook!.missingSessionNotice).toBeNull();
    expect(captured.hook!.historyLoadError).toBeNull();
  });
});
