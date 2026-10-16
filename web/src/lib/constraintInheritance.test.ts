// @vitest-environment jsdom
/** constraintInheritance lib 单测（#1192 Phase 3）。
 *
 *  锁的是：① 事件流 → 快照投影的形状安全解析（坏数据容错不 throw、
 *  不伪造）；② 无快照事件返回 null（数据不存在就不渲染）；③ scope 眉标
 *  与 ConstraintsTab 同源词汇。 */

import { describe, expect, it } from 'vitest';
import type { AgentEvent } from '../types';
import {
  CONSTRAINT_INHERITANCE_EVENT_TYPE,
  parseInheritanceEvent,
  scopeEyebrow,
} from './constraintInheritance';

function ev(data: unknown): AgentEvent {
  return { type: CONSTRAINT_INHERITANCE_EVENT_TYPE, data, seq: 1, run_id: null, step_id: null } as unknown as AgentEvent;
}

const ROW = {
  fact_id: 'f-1',
  type: 'constraint',
  value: '所有 Python 改动必须先写 failing test',
  scope: 'project',
  source_event_id: 'scoped:f-1',
  source_seq: 0,
};

describe('parseInheritanceEvent', () => {
  it('returns null when no inheritance event exists', () => {
    expect(parseInheritanceEvent([])).toBeNull();
    expect(parseInheritanceEvent([{ type: 'user/message', data: {}, seq: 0 } as unknown as AgentEvent])).toBeNull();
  });

  it('projects inherited and not_inherited rows', () => {
    const snap = parseInheritanceEvent([
      ev({ omit_constraints: false, inherited: [ROW], not_inherited: [{ ...ROW, fact_id: 'f-2', scope: 'session' }] }),
    ]);
    expect(snap).not.toBeNull();
    expect(snap!.omitConstraints).toBe(false);
    expect(snap!.inherited).toEqual([
      { factId: 'f-1', type: 'constraint', value: '所有 Python 改动必须先写 failing test', scope: 'project' },
    ]);
    expect(snap!.notInherited).toEqual([
      { factId: 'f-2', type: 'constraint', value: '所有 Python 改动必须先写 failing test', scope: 'session' },
    ]);
  });

  it('projects omit_constraints=true with empty inherited', () => {
    const snap = parseInheritanceEvent([ev({ omit_constraints: true, inherited: [], not_inherited: [ROW] })]);
    expect(snap!.omitConstraints).toBe(true);
    expect(snap!.inherited).toEqual([]);
    expect(snap!.notInherited).toHaveLength(1);
  });

  it('tolerates malformed rows and arrays without throwing', () => {
    const snap = parseInheritanceEvent([
      ev({
        omit_constraints: false,
        inherited: [null, 'x', 42, { fact_id: '', type: 'constraint' }, { fact_id: 'f-3', type: 'authorization', value: 7, scope: 'bogus' }, ROW],
        not_inherited: 'not-an-array',
      }),
    ]);
    expect(snap!.inherited).toEqual([
      { factId: 'f-3', type: 'authorization', value: '7', scope: 'session' },
      { factId: 'f-1', type: 'constraint', value: '所有 Python 改动必须先写 failing test', scope: 'project' },
    ]);
    expect(snap!.notInherited).toEqual([]);
  });

  it('returns null for non-object event data', () => {
    expect(parseInheritanceEvent([ev(null)])).toBeNull();
    expect(parseInheritanceEvent([ev([1, 2])])).toBeNull();
  });

  it('takes the first matching event', () => {
    const snap = parseInheritanceEvent([
      ev({ omit_constraints: false, inherited: [ROW], not_inherited: [] }),
      ev({ omit_constraints: true, inherited: [], not_inherited: [] }),
    ]);
    expect(snap!.omitConstraints).toBe(false);
    expect(snap!.inherited).toHaveLength(1);
  });
});

describe('scopeEyebrow', () => {
  it('matches mockup wording with SCOPE_LABELS vocabulary', () => {
    expect(scopeEyebrow('session')).toBe('SESSION · 本会话');
    expect(scopeEyebrow('project')).toBe('PROJECT · 本项目');
    expect(scopeEyebrow('global')).toBe('GLOBAL · 我的全局');
  });
});
