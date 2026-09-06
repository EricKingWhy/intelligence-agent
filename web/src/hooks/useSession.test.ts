/** useSession 流式-模式一致性契约——纯函数锁不变量 #22。
 *
 * 关注的 bug 形态：SSE 在途帧晚于 cancel / selectSession / onError 到达，
 * 仍然写 conversation 而覆盖刚加载的目标会话视图。提取一个纯函数
 * 决定「当前模式是否仍是该流的权威消费者」，所有回调路径据此短路。
 *
 * 这一层的契约只能用纯函数锁——React 状态机本身需要 testing-library，
 * 引入它会扩大 scope；提取是真正的深模块化（参 codebase-design）。
 */

import { describe, expect, it, vi } from 'vitest';
import type { AgentEvent, SessionMode } from '../types';
import { createCommitCoalescer, shouldApplyRecoverResult, shouldApplyStreamFrame } from './useSession';

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

describe('shouldApplyRecoverResult — recover 响应是否仍是当前模式的权威真相', () => {
  // 同 shouldApplyStreamFrame 家族的 stale-write 守护：recover pending 期间
  // 用户可能已切走（selectSession / submitTask / cancelStream），晚到的 200
  // 响应不得覆盖刚加载的目标会话视图（不变量 #22）。

  it('viewing(sid) 且 sid 匹配：应用重建结果（正常路径）', () => {
    const mode: SessionMode = { kind: 'viewing', sessionId: 'A' };
    expect(shouldApplyRecoverResult(mode, 'A')).toBe(true);
  });

  it('viewing(其他 sid)：拒绝——pending 期间已切到别的会话，旧响应不得覆盖', () => {
    const mode: SessionMode = { kind: 'viewing', sessionId: 'B' };
    expect(shouldApplyRecoverResult(mode, 'A')).toBe(false);
  });

  it('live：拒绝——pending 期间已开始新流，recover 结果与当前视图无关', () => {
    const mode: SessionMode = { kind: 'live', sessionId: null };
    expect(shouldApplyRecoverResult(mode, 'A')).toBe(false);
    const modeLive = { kind: 'live', sessionId: 'A' } as const;
    expect(shouldApplyRecoverResult(modeLive, 'A')).toBe(false);
  });

  it('idle：拒绝——已无持有会话', () => {
    const mode: SessionMode = { kind: 'idle' };
    expect(shouldApplyRecoverResult(mode, 'A')).toBe(false);
  });
});

describe('createCommitCoalescer — P1-3 delta 合帧提交（HANDOFF §6）', () => {
  it('窗口内多次 schedule 只触发一次提交', () => {
    vi.useFakeTimers();
    try {
      let commits = 0;
      const c = createCommitCoalescer(() => commits++, 24);
      c.schedule();
      c.schedule();
      c.schedule();
      expect(commits).toBe(0);
      vi.advanceTimersByTime(24);
      expect(commits).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('flush 立即提交待合帧数据并取消挂起定时器（流终止尾帧不丢、不重复）', () => {
    vi.useFakeTimers();
    try {
      let commits = 0;
      const c = createCommitCoalescer(() => commits++, 24);
      c.schedule();
      c.flush();
      expect(commits).toBe(1);
      vi.advanceTimersByTime(100);
      expect(commits).toBe(1); // 定时器已取消，不重复提交
    } finally {
      vi.useRealTimers();
    }
  });

  it('无待提交数据时 flush 是 no-op', () => {
    let commits = 0;
    const c = createCommitCoalescer(() => commits++, 24);
    c.flush();
    expect(commits).toBe(0);
  });

  it('cancel 丢弃挂起定时器（cancel 流后不再迟到提交）', () => {
    vi.useFakeTimers();
    try {
      let commits = 0;
      const c = createCommitCoalescer(() => commits++, 24);
      c.schedule();
      c.cancel();
      vi.advanceTimersByTime(100);
      expect(commits).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
