// @vitest-environment jsdom
/** #1256 `useDelegations` — roster 数据源行为契约。
 *
 *  钉三件事：
 *   ① sessionId 为 null 不请求（空 roster）；
 *   ② 父流委派签名变化（新委派出现 / status 翻转）触发重拉——不用轮询页面；
 *   ③ 读失败 → 空 roster（不伪造，PRODUCT「真实优先于好看」）。
 *
 *  会话用真投影（initConversation + applyEvent）造，别手搓 ConversationState——
 *  签名读的是投影产物 `turns[].delegations[]`，真投影才验证得动。
 *  `../lib/api` 只替换 `getSessionDelegations`，其余走真实实现。
 */

import { act, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventType } from '../types';
import type { AgentEvent, ConversationState } from '../types';
import type { DelegationEntry } from '../lib/api';

const H = vi.hoisted(() => ({
  getSessionDelegations: vi.fn(),
}));

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, getSessionDelegations: H.getSessionDelegations };
});

import { applyEvent, initConversation } from '../lib/projection';
import { useDelegations } from './useDelegations';

const SID = 'p';
const T = '2026-10-16T00:00:00.000Z';

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  return { data: {}, seq: null, run_id: 'r1', step_id: 1, session_id: SID, time: T, ...partial } as AgentEvent;
}

/** 造一段「有一轮 + N 个委派」的父会话。 */
function conversation(children: string[]): ConversationState {
  let s = initConversation(SID);
  s = applyEvent(s, ev({ type: EventType.SESSION_STARTED, seq: 1 }));
  s = applyEvent(s, ev({ type: EventType.RUN_STARTED, seq: 2 }));
  s = applyEvent(s, ev({ type: EventType.USER_MESSAGE, seq: 3, data: { content: 'go', step: 1 } }));
  children.forEach((child, i) => {
    s = applyEvent(s, ev({
      type: EventType.AGENT_DELEGATION_STARTED, seq: 4 + i,
      data: { target: 'research_review', task: 't', child_session_id: child },
    }));
  });
  return s;
}

function entry(child: string): DelegationEntry {
  return {
    child_session_id: child, target: 'research_review', task: 't',
    status: 'running', started_at: T, completed_at: null,
    used_tokens: 0, usage_state: 'no_data',
  };
}

let host: HTMLDivElement;
let root: Root;
let current: DelegationEntry[] | undefined;

function Harness({ sessionId, conv }: { sessionId: string | null; conv: ConversationState | null }) {
  const rows = useDelegations(sessionId, conv);
  useEffect(() => { current = rows; }, [rows]);
  return null;
}

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  H.getSessionDelegations.mockReset();
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  current = undefined;
  vi.unstubAllGlobals();
});

describe('#1256 useDelegations', () => {
  it('sessionId 为 null：不请求，roster 为空', async () => {
    await act(async () => { root.render(<Harness sessionId={null} conv={null} />); });
    await flush();
    expect(H.getSessionDelegations).not.toHaveBeenCalled();
    expect(current).toEqual([]);
  });

  it('拉到行：原样交回（状态由后端投影，hook 不改写）', async () => {
    H.getSessionDelegations.mockResolvedValue([entry('c1')]);
    await act(async () => { root.render(<Harness sessionId={SID} conv={conversation(['c1'])} />); });
    await flush();
    expect(H.getSessionDelegations).toHaveBeenCalledWith(SID);
    expect(current).toEqual([entry('c1')]);
  });

  it('父流委派签名变化（新委派出现）→ 重拉', async () => {
    H.getSessionDelegations.mockResolvedValue([entry('c1')]);
    await act(async () => { root.render(<Harness sessionId={SID} conv={conversation(['c1'])} />); });
    await flush();
    const first = H.getSessionDelegations.mock.calls.length;

    H.getSessionDelegations.mockResolvedValue([entry('c1'), entry('c2')]);
    await act(async () => { root.render(<Harness sessionId={SID} conv={conversation(['c1', 'c2'])} />); });
    await flush();

    expect(H.getSessionDelegations.mock.calls.length).toBeGreaterThan(first);
    expect(current).toEqual([entry('c1'), entry('c2')]);
  });

  it('读失败 → 空 roster（不伪造行）', async () => {
    H.getSessionDelegations.mockRejectedValue(new Error('boom'));
    await act(async () => { root.render(<Harness sessionId={SID} conv={conversation(['c1'])} />); });
    await flush();
    expect(current).toEqual([]);
  });

  it('非数组响应（畸形 / 老后端）→ 空 roster，不把对象当行列表', async () => {
    const rows = {} as unknown as DelegationEntry[];
    H.getSessionDelegations.mockResolvedValue(rows);
    await act(async () => { root.render(<Harness sessionId={SID} conv={conversation(['c1'])} />); });
    await flush();
    expect(current).toEqual([]);
  });
});
