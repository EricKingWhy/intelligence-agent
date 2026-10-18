/** 恢复 UI 纯函数契约（`#1327` 缩减版）。
 *
 *  `#1327` 删除了「UNKNOWN 操作人工裁决」的前端半边（`DECISION_OPTIONS` /
 *  `assembleDecisions` / `initialDrafts` / `probeFact` / `humanizeToolName` /
 *  `SOURCE_PRESETS` 随 `RecoveryDecisionPanel` 一起删；裁决改由后端按操作类型
 *  自动做，前端没有可组装的裁决载荷）。测已删函数的旧 describe 已随之删除——
 *  没有消费方的纯函数不值得留测试当墓碑（不变量 #22 的同向纪律）。
 *
 *  本文件只钉住还活着的另一半：恢复列表（`RecoveryListPanel`）用的进度文件
 *  版本文案（`formatProgressVersion` / `progressTone`）——缺失/不可读/不匹配
 *  必须如实标，绝不伪造「最新」。
 */

import { describe, expect, it } from 'vitest';
import { formatProgressVersion, progressTone } from './recovery';

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
