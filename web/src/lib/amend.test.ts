/** amend.ts 契约测试——Composer 档位 → 提交字段的单一映射点。
 *
 *  锁定两条边界：
 *  1. 字段集：续聊 amend 面不含 permission_mode（不在 /messages 契约内）；
 *     创建路径包含它。
 *  2. 归一化归属：本模块**不做**空值丢弃——空值映射成 undefined，丢弃由 api 层
 *     单一执行（见 amend.ts 顶部契约）。这条边界一旦模糊，就会回到
 *     「两层各判一次空、谁拥有契约说不清」的旧状态。
 *
 *  #201：`context_providers` 已从本模块下线（多选控件删除）。下面第三条断言锁的是
 *  **映射层不再产出这个键**——不传键 = 后端默认（全部已装配 provider），与删除前
 *  "未选"时的行为一致。 */

import { describe, expect, it } from 'vitest';
import { toAmendFields, toCreateControls, type ComposerControls } from './amend';

const EMPTY: ComposerControls = {
  model: null,
  permissionMode: null,
  agentProfile: null,
  reasoningEffort: null,
};

const FULL: ComposerControls = {
  model: 'glm-4.5',
  permissionMode: 'auto',
  agentProfile: 'coding',
  reasoningEffort: 'deep',
};

describe('toAmendFields — 续聊 amend 面（三项，无 permission_mode）', () => {
  it('全有值 → camelCase 映射为契约字段名，不含 permission_mode', () => {
    const fields = toAmendFields(FULL);
    expect(fields).toEqual({
      model: 'glm-4.5',
      agent_profile: 'coding',
      reasoning_effort: 'deep',
    });
    expect(fields).not.toHaveProperty('permission_mode');
  });

  it('null 档位 → undefined（映射层不判空，交给 api 层丢弃）', () => {
    const fields = toAmendFields(EMPTY);
    expect(fields.model).toBeUndefined();
    expect(fields.agent_profile).toBeUndefined();
    expect(fields.reasoning_effort).toBeUndefined();
  });

  it('不再产出 context_providers 键（#201 下线多选控件；不传键 = 后端默认）', () => {
    expect(toAmendFields(FULL)).not.toHaveProperty('context_providers');
    expect(toAmendFields(EMPTY)).not.toHaveProperty('context_providers');
  });
});

describe('toCreateControls — 创建路径控制面（amend 三项 + permission_mode）', () => {
  it('全有值 → 含 permission_mode，不含 context_providers', () => {
    const controls = toCreateControls(FULL);
    expect(controls).toEqual({
      model: 'glm-4.5',
      permission_mode: 'auto',
      agent_profile: 'coding',
      reasoning_effort: 'deep',
    });
    expect(controls).not.toHaveProperty('context_providers');
  });

  it('空档位 → 各字段 undefined（api 层丢弃后即「不传键」）', () => {
    expect(toCreateControls(EMPTY).permission_mode).toBeUndefined();
    expect(toCreateControls(EMPTY).model).toBeUndefined();
  });
});
