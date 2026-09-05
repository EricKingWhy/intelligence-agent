/** useSession 流式-模式一致性契约——纯函数锁不变量 #22。
 *
 * 关注的 bug 形态：SSE 在途帧晚于 cancel / selectSession / onError 到达，
 * 仍然写 conversation 而覆盖刚加载的目标会话视图。提取一个纯函数
 * 决定「当前模式是否仍是该流的权威消费者」，所有回调路径据此短路。
 *
 * 这一层的契约只能用纯函数锁——React 状态机本身需要 testing-library，
 * 引入它会扩大 scope；提取是真正的深模块化（参 codebase-design）。
 */

import { describe, expect, it } from 'vitest';
import type { AgentEvent, SessionMode } from '../types';
import { shouldApplyStreamFrame } from './useSession';

const ev = (type: string, session_id: string | null): AgentEvent => ({
  type,
  data: {},
  seq: null,
  run_id: null,
  step_id: null,
  session_id: session_id ?? undefined,
});

describe('shouldApplyStreamFrame — 流式帧是否仍是当前模式的权威真相', () => {
  it('live(null) 初始态：任意帧都应用（首帧尚未确定 sid）', () => {
    const mode: SessionMode = { kind: 'live', sessionId: null };
    expect(shouldApplyStreamFrame(mode, ev('run/started', 'A'))).toBe(true);
    expect(shouldApplyStreamFrame(mode, ev('run/started', null))).toBe(true);
  });

  it('live(sid) + 匹配 sid：应用', () => {
    const mode: SessionMode = { kind: 'live', sessionId: 'A' };
    expect(shouldApplyStreamFrame(mode, ev('model/delta', 'A'))).toBe(true);
  });

  it('live(sid) + 不同 sid：拒绝——旧流窜入新会话属于不变量违反', () => {
    const mode: SessionMode = { kind: 'live', sessionId: 'A' };
    expect(shouldApplyStreamFrame(mode, ev('model/delta', 'B'))).toBe(false);
  });

  it('viewing(sid) 模式：一律拒绝——流已不再是权威消费者', () => {
    // 这是「切会话 / 取消」之后的核心保护：迟到帧不能覆盖刚加载的历史视图。
    const mode: SessionMode = { kind: 'viewing', sessionId: 'A' };
    expect(shouldApplyStreamFrame(mode, ev('model/delta', 'A'))).toBe(false);
    expect(shouldApplyStreamFrame(mode, ev('model/delta', 'B'))).toBe(false);
  });

  it('idle 模式：一律拒绝——已无持有会话', () => {
    const mode: SessionMode = { kind: 'idle' };
    expect(shouldApplyStreamFrame(mode, ev('run/started', 'A'))).toBe(false);
  });

  it('live(sid) + 帧 session_id 缺省（首帧未带 sid）：仍应用——sid 首帧才落定', () => {
    const mode: SessionMode = { kind: 'live', sessionId: 'A' };
    expect(shouldApplyStreamFrame(mode, ev('model/delta', null))).toBe(true);
  });
});
