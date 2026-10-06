/** #357 W-13 恢复 UI 的纯函数契约（先红后绿）。
 *
 *  这一层刻意**不碰 React / DOM**：默认动作、来源采集、进度版本文案这类判据
 *  都能脱离渲染单测，组件只做「把状态摆到屏幕上」。任何与后端 `pending_decisions`
 *  有关的映射都在这里钉住——UI 不维护第二套真相（不变量 #22）。
 */

import { describe, expect, it } from 'vitest';
import {
  assembleDecisions,
  DECISION_OPTIONS,
  formatProgressVersion,
  humanizeToolName,
  initialDraft,
  initialDrafts,
  probeFact,
  progressTone,
  RECOVERY_SOURCE_MAX,
  SOURCE_PRESETS,
} from './recovery';
import { RECOVER_DECISION_SOURCE_MAX, type PendingDecision } from './api';

function decision(overrides: Partial<PendingDecision> = {}): PendingDecision {
  return {
    tool_call_id: 'call-1',
    tool_name: 'git_status',
    state: 'NEED_RECONCILE',
    default_action: 'RETRY',
    risk_level: 'low',
    probe: { verifiable: true, suggested_action: '重新运行 git status，核对工作区状态。' },
    ...overrides,
  };
}

describe('humanizeToolName — 最小可读化，不建第二套工具注册表', () => {
  it('下划线转空格', () => {
    expect(humanizeToolName('git_status')).toBe('git status');
    expect(humanizeToolName('apply_patch')).toBe('apply patch');
  });

  it('无下划线原样返回（不猜别名）', () => {
    expect(humanizeToolName('bash')).toBe('bash');
  });

  it('空串如实回落占位，不编造工具名', () => {
    expect(humanizeToolName('')).toBe('未命名工具');
  });
});

describe('DECISION_OPTIONS — 三选一，默认项由后端 default_action 决定', () => {
  it('恰好三个选项，verdict 与后端词表一致', () => {
    expect(DECISION_OPTIONS.map((o) => o.verdict)).toEqual(['CONFIRM_SUCCESS', 'RETRY', 'DEFER']);
  });

  it('每项都有非空 label 与 help（后果语言，无开放技术问答）', () => {
    for (const option of DECISION_OPTIONS) {
      expect(option.label.length).toBeGreaterThan(0);
      expect(option.help.length).toBeGreaterThan(0);
    }
  });

  it('来源预设是两条通俗可选项', () => {
    expect(SOURCE_PRESETS).toEqual(['我亲眼看到结果了', '我查了外部系统']);
  });

  it('来源上限与 api.ts 的 wire 契约同口径', () => {
    expect(RECOVERY_SOURCE_MAX).toBe(RECOVER_DECISION_SOURCE_MAX);
  });
});

describe('probeFact — 只转述后端 hint，不伪造结论', () => {
  it('可核验且有建议 → 已查到 + 建议原文', () => {
    expect(probeFact({ verifiable: true, suggested_action: '读回文件核对。' })).toEqual({
      state: '已查到',
      detail: '读回文件核对。',
    });
  });

  it('不可核验 → 未查到，且不带任何猜测', () => {
    expect(probeFact({ verifiable: false, suggested_action: '也许成功了' })).toEqual({
      state: '未查到',
      detail: null,
    });
  });

  it('可核验但无建议 → 未查到（缺建议等于没法核对）', () => {
    expect(probeFact({ verifiable: true, suggested_action: null })).toEqual({
      state: '未查到',
      detail: null,
    });
  });
});

describe('initialDraft / initialDrafts — 默认选中最安全项', () => {
  it('default_action=DEFER 时默认 DEFER', () => {
    expect(initialDraft(decision({ default_action: 'DEFER', risk_level: 'high' })).verdict).toBe('DEFER');
  });

  it('default_action=RETRY 时默认 RETRY', () => {
    expect(initialDraft(decision({ default_action: 'RETRY', risk_level: 'low' })).verdict).toBe('RETRY');
  });

  it('不带来源预填（来源是用户自陈，不能替用户编）', () => {
    const draft = initialDraft(decision());
    expect(draft.sourceChoice).toBe('');
    expect(draft.sourceCustom).toBe('');
  });

  it('initialDrafts 以 tool_call_id 为键', () => {
    const drafts = initialDrafts([decision({ tool_call_id: 'a' }), decision({ tool_call_id: 'b' })]);
    expect(Object.keys(drafts).sort()).toEqual(['a', 'b']);
  });
});

describe('assembleDecisions — 组装 POST /recover 的 wire 载荷', () => {
  it('逐条取各卡 verdict，不带来源时不发 source 键', () => {
    const cards = [decision({ tool_call_id: 'a' }), decision({ tool_call_id: 'b', default_action: 'DEFER' })];
    const drafts = initialDrafts(cards);
    expect(assembleDecisions(cards, drafts)).toEqual([
      { tool_call_id: 'a', verdict: 'RETRY', source: undefined },
      { tool_call_id: 'b', verdict: 'DEFER', source: undefined },
    ]);
  });

  it('CONFIRM_SUCCESS 才采集来源：预设原样进载荷', () => {
    const cards = [decision({ tool_call_id: 'a' })];
    const drafts = initialDrafts(cards);
    drafts.a = { verdict: 'CONFIRM_SUCCESS', sourceChoice: '我查了外部系统', sourceCustom: '' };
    expect(assembleDecisions(cards, drafts)).toEqual([
      { tool_call_id: 'a', verdict: 'CONFIRM_SUCCESS', source: '我查了外部系统' },
    ]);
  });

  it('自定义来源优先于预设（并去掉首尾空白）', () => {
    const cards = [decision({ tool_call_id: 'a' })];
    const drafts = initialDrafts(cards);
    drafts.a = { verdict: 'CONFIRM_SUCCESS', sourceChoice: '我亲眼看到结果了', sourceCustom: '  看了 DB  ' };
    expect(assembleDecisions(cards, drafts)[0].source).toBe('看了 DB');
  });

  it('非 CONFIRM_SUCCESS 裁决即使有来源文本也不发 source（来源只服务「确认成功」）', () => {
    const cards = [decision({ tool_call_id: 'a' })];
    const drafts = initialDrafts(cards);
    drafts.a = { verdict: 'RETRY', sourceChoice: '我查了外部系统', sourceCustom: '' };
    expect(assembleDecisions(cards, drafts)[0].source).toBeUndefined();
  });

  it('某卡缺 draft 时回落该卡默认 verdict（不整批丢）', () => {
    const cards = [decision({ tool_call_id: 'a', default_action: 'DEFER' })];
    expect(assembleDecisions(cards, {})).toEqual([{ tool_call_id: 'a', verdict: 'DEFER', source: undefined }]);
  });

  it('来源超过上限时抛错（不静默截断——审计留痕不得伪造）', () => {
    const cards = [decision({ tool_call_id: 'a' })];
    const drafts = initialDrafts(cards);
    drafts.a = { verdict: 'CONFIRM_SUCCESS', sourceChoice: '', sourceCustom: 'x'.repeat(RECOVERY_SOURCE_MAX + 1) };
    expect(() => assembleDecisions(cards, drafts)).toThrow(/过长|上限/);
  });
});

describe('formatProgressVersion / progressTone — 缺失/不可读/不匹配如实标，不伪造「最新」', () => {
  it('可读 → 版本号 + 事件序号', () => {
    expect(formatProgressVersion({ schema_version: '1', source_event_seq: 42 })).toContain('1');
    expect(formatProgressVersion({ schema_version: '1', source_event_seq: 42 })).toContain('42');
  });

  it('缺失 → 未知 + 不谎称「最新」', () => {
    const text = formatProgressVersion({ status: 'missing', reason: 'no file' });
    expect(text).toContain('未知');
    expect(text).not.toContain('最新');
  });

  it('不可读/版本不匹配 → 各自如实区分', () => {
    expect(formatProgressVersion({ status: 'unreadable', reason: 'perm' })).toContain('不可读');
    expect(formatProgressVersion({ status: 'invalid_schema', reason: 'v9' })).toContain('不匹配');
  });

  it('tone：可读为 ok，其余为 unknown', () => {
    expect(progressTone({ schema_version: '1', source_event_seq: 1 })).toBe('ok');
    expect(progressTone({ status: 'missing', reason: '' })).toBe('unknown');
  });
});
