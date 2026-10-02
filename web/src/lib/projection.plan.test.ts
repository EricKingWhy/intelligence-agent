/** #381（W-27）：`task/plan_updated` 投影——整表覆盖 last-wins + 行级容错。
 *
 * 契约：docs/PRD_LONG_TASK_CONTEXT_MANAGEMENT.md §7.1-7.3（五字段、整表覆盖、
 * 软上限 50）；后端 `session/plan.py` handler 已硬校验（单 in_progress / 状态机 /
 * id 唯一），前端投影只防「手写 / 污染 JSONL 的旧数据」——一行坏数据只损失该行，
 * 与后端 `derive_plan` 同哲学。状态机级违规（如双 in_progress）**不改写、原样
 * 保留**，渲染端容错归 PlanList 组件（票面指令）。
 */
import { describe, expect, it } from 'vitest';
import { applyEvent, initConversation, projectHistory } from './projection';
import { EventType, type AgentEvent } from '../types';

const planEvent = (items: unknown[], seq: number): AgentEvent =>
  ({
    type: EventType.TASK_PLAN_UPDATED,
    data: { items },
    seq,
    run_id: 'run-1',
    step_id: null,
    time: '2026-10-02T00:00:00Z',
  }) as unknown as AgentEvent;

const item = (id: string, status: string) => ({
  id,
  content: `任务 ${id}`,
  activeForm: `正在执行 ${id}`,
  status,
  source: 'agent',
});

describe('#381（W-27）：task/plan_updated 投影', () => {
  it('首帧写入：五字段逐字进 state.plan', () => {
    const state = applyEvent(initConversation('s1'), planEvent([item('a', 'in_progress')], 1));
    expect(state.plan).toEqual([item('a', 'in_progress')]);
  });

  it('整表覆盖 last-wins：第二帧整体替换第一帧（含缩短与 id 变化）', () => {
    let state = applyEvent(initConversation('s1'), planEvent([item('a', 'completed'), item('b', 'in_progress')], 1));
    state = applyEvent(state, planEvent([item('x', 'pending')], 2));
    expect(state.plan).toEqual([item('x', 'pending')]);
  });

  it('空 items = 合法清空：plan 变 []（渲染面归组件不留空壳）', () => {
    let state = applyEvent(initConversation('s1'), planEvent([item('a', 'pending')], 1));
    state = applyEvent(state, planEvent([], 2));
    expect(state.plan).toEqual([]);
  });

  it('行级容错：非对象行、缺 id 行丢弃，好行保留', () => {
    const state = applyEvent(
      initConversation('s1'),
      planEvent([null, 'garbage', { content: '没 id' }, { id: '', content: '空 id' }, item('ok', 'pending')], 1),
    );
    expect(state.plan).toEqual([item('ok', 'pending')]);
  });

  it('重复 id：首个胜（key 唯一性；服务端本不该发，防手写 JSONL）', () => {
    const state = applyEvent(
      initConversation('s1'),
      planEvent([item('a', 'pending'), { ...item('a', 'completed') }], 1),
    );
    expect(state.plan).toEqual([item('a', 'pending')]);
  });

  it('字段归一不猜测：content 非字符串取空串、activeForm 缺失回落 content、status 非字符串记空', () => {
    const state = applyEvent(
      initConversation('s1'),
      planEvent([{ id: 'a', content: 42, status: 7 }, { id: 'b', content: '任务 b', activeForm: '', status: 'pending' }], 1),
    );
    expect(state.plan).toEqual([
      { id: 'a', content: '', activeForm: '', status: '', source: '' },
      { id: 'b', content: '任务 b', activeForm: '任务 b', status: 'pending', source: '' },
    ]);
  });

  it('状态机违规原样保留：双 in_progress 不被投影改写（渲染端容错归组件）', () => {
    const state = applyEvent(
      initConversation('s1'),
      planEvent([item('a', 'in_progress'), item('b', 'in_progress')], 1),
    );
    expect(state.plan).toEqual([item('a', 'in_progress'), item('b', 'in_progress')]);
  });

  it('items 非数组 = 坏帧整个忽略：plan 保持原值', () => {
    let state = applyEvent(initConversation('s1'), planEvent([item('a', 'pending')], 1));
    state = applyEvent(state, planEvent('garbage' as unknown as unknown[], 2));
    expect(state.plan).toEqual([item('a', 'pending')]);
  });

  it('重放确定性：projectHistory 与逐帧 applyEvent 得到同一 plan', () => {
    const events = [
      planEvent([item('a', 'completed'), item('b', 'in_progress')], 3),
      planEvent([item('a', 'completed'), item('b', 'completed'), item('c', 'pending')], 7),
    ];
    const viaHistory = projectHistory('s1', events);
    const viaApply = events.reduce((st, e) => applyEvent(st, e), initConversation('s1'));
    expect(viaHistory.plan).toEqual(viaApply.plan);
    expect(viaHistory.plan?.map((i) => i.id)).toEqual(['a', 'b', 'c']);
  });

  it('initConversation：plan 初始为 null（未出现过清单 ≠ 清空）', () => {
    expect(initConversation('s1').plan).toBeNull();
  });
});
