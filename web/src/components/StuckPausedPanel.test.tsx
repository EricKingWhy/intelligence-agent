/** StuckPausedPanel 渲染（`#968`）。
 *
 *  静态渲染（renderToStaticMarkup，同 PausedPanel.test.tsx 的口径）：锁的是
 *  stuck 暂停面板的**行为差异**——
 *  ①不提供 budget-increase 恢复控件（无「恢复同一 run」按钮、无 ceiling 输入框、
 *    无「绝对 ceiling」字样；`11 §6.1`：stuck 暂停 MUST NOT 提供无依据的一键重试）；
 *  ②提供 steer 引导：给「新指示（steer）」入口的说明，并指明用下方输入框 +
 *    Ctrl/Cmd+Enter 发送（Composer 不被面板禁用/遮挡）；
 *  ③恢复前置条件（resume_requirements 非空）如实列出。
 *
 *  提交 steer / 409 刷新保输入由 App/useSession 层负责，不在本文件。 */

import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { StuckPausedPanel } from './StuckPausedPanel';
import type { RunPausedInfo } from '../types';

function stuckPaused(overrides: Partial<RunPausedInfo> = {}): RunPausedInfo {
  return {
    run_id: 'run-stuck',
    version: 5,
    pause_seq: 12,
    reason: 'stuck',
    trigger_dimension: 'stuck.same_name_same_args',
    consumed_agent_turns: 7,
    run_limit: 20,
    consumed_dimensions: null,
    run_limits: null,
    local_fuse: { max_agent_turns: 500, source: 'deployment' },
    continuation: {
      completed: ['检测器连续 3 次观测到同工具同参数调用'],
      remaining: ['等待用户给出变更依据'],
      blockers: ['重复模式在 replan 后仍持续'],
      next_safe_action: '提供相关 steer / 环境变更 / 策略变更后恢复',
    },
    closeout_source: 'deterministic',
    resume_requirements: ['需要相关 steer，或已记录的环境 / policy 变更'],
    trace_id: 'trace-stuck',
    ...overrides,
  };
}

function render(p: RunPausedInfo): string {
  return renderToStaticMarkup(<StuckPausedPanel paused={p} />);
}

describe('StuckPausedPanel — stuck 暂停的引导面板（#968）', () => {
  it('不含 budget-increase 恢复控件：无恢复按钮、无 ceiling 输入框、无「绝对 ceiling」字样', () => {
    const html = render(stuckPaused());
    expect(html).not.toContain('恢复同一 run');
    expect(html).not.toContain('pause-resume-ceiling');
    expect(html).not.toContain('绝对 ceiling');
  });

  it('含 steer 引导：指明在下方输入框输入并按 Ctrl/Cmd+Enter 发送', () => {
    const html = render(stuckPaused());
    expect(html).toContain('steer');
    expect(html).toContain('新指示');
    expect(html).toContain('在下方输入框输入，按 Ctrl/Cmd+Enter 发送');
  });

  it('展示恢复前置条件（非空 resume_requirements 复用「恢复前置条件：」措辞）', () => {
    const html = render(stuckPaused());
    expect(html).toContain('恢复前置条件：');
    expect(html).toContain('需要相关 steer，或已记录的环境 / policy 变更');
  });

  it('状态行沿用「不是失败」措辞惯例（stuck 是非终态收口）', () => {
    const html = render(stuckPaused());
    expect(html).toContain('不是失败');
  });

  it('事实行报原因标签 / 预算版本 / trace（与 pause-panel 同一投影口径）', () => {
    const html = render(stuckPaused());
    expect(html).toContain('疑似卡住');
    expect(html).toContain('预算版本 5');
    expect(html).toContain('trace-stuck');
  });

  it('continuation 存在时如实呈现（completed / blockers / 下一步）', () => {
    const html = render(stuckPaused());
    expect(html).toContain('检测器连续 3 次观测到同工具同参数调用');
    expect(html).toContain('重复模式在 replan 后仍持续');
    expect(html).toContain('下一步：提供相关 steer / 环境变更 / 策略变更后恢复');
  });
});
