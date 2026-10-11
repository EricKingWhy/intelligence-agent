import { describe, expect, it } from 'vitest';
import type { ModelCatalogEntry } from './api';
import { modelChangeTarget, normalizeSelectedModel, selectableModels } from './modelSelection';

const entry = (name: string, isDefault = false): ModelCatalogEntry => ({
  name,
  provider: 'p',
  model: name,
  default: isDefault,
});

const catalog = [
  entry('deepseek-v4-flash-0731', true),
  entry('glm-5.3-flash'),
  entry('qwen-plus'),
];

describe('modelChangeTarget（FE-R11-02：选「默认链」也要能清掉会话级覆盖）', () => {
  it('选具体模型 → 原样返回该模型名', () => {
    expect(modelChangeTarget('glm-5.3-flash', catalog)).toBe('glm-5.3-flash');
  });

  it('选「默认链」（null）→ 解析成 catalog 的 default 条目名（后端据此清覆盖）', () => {
    // 后端 is_default_selection() 命中该名字 → model/changed{to=None}
    expect(modelChangeTarget(null, catalog)).toBe('deepseek-v4-flash-0731');
  });

  it('catalog 里没有 default 条目 → null（调用方保持既有行为，不伪造目标）', () => {
    expect(modelChangeTarget(null, [entry('glm-5.3-flash')])).toBeNull();
  });

  it('catalog 为空 → null（端点缺席时选择器本就降级隐藏）', () => {
    expect(modelChangeTarget(null, [])).toBeNull();
  });

  it('第一个 default 条目胜出（后端只保证存在，不保证唯一）', () => {
    expect(modelChangeTarget(null, [entry('a', true), entry('b', true)])).toBe('a');
  });
});

describe('normalizeSelectedModel（#1016：默认条目名归一为 null，不固化成模型名）', () => {
  it('默认条目名 → null（不得固化成默认模型名）', () => {
    expect(normalizeSelectedModel('deepseek-v4-flash-0731', catalog)).toBeNull();
  });

  it('普通模型名 → 原样返回', () => {
    expect(normalizeSelectedModel('glm-5.3-flash', catalog)).toBe('glm-5.3-flash');
  });

  it('null（一级「默认链」）→ null', () => {
    expect(normalizeSelectedModel(null, catalog)).toBeNull();
  });

  it('目录里不认识的名字 → 原样返回（不认识就说不认识，不假装是默认）', () => {
    expect(normalizeSelectedModel('removed-model', catalog)).toBe('removed-model');
  });

  it('目录未加载（空数组）→ 原样返回（无法判默认性，不乱归零，审查 P2）', () => {
    expect(normalizeSelectedModel('deepseek-v4-flash-0731', [])).toBe('deepseek-v4-flash-0731');
  });

  it('消融实验 4：默认条目与同名 catalog 条目折叠成一个符号时 → 仍归 null', () => {
    // 后端此时让默认条目优先；前端据同一条 `default` 标记归一，语义一致。
    const collapsed = [
      entry('deepseek-chat', true), // 默认条目（MODEL_NAME == catalog 条目名）
    ];
    expect(normalizeSelectedModel('deepseek-chat', collapsed)).toBeNull();
  });
});

describe('selectableModels（#1016：默认条目不进二级列表）', () => {
  it('剔除默认条目，其余原样保留', () => {
    const names = selectableModels(catalog).map((m) => m.name);
    expect(names).toEqual(['glm-5.3-flash', 'qwen-plus']);
    expect(names).not.toContain('deepseek-v4-flash-0731');
  });

  it('无默认条目 → 原样返回', () => {
    const only = [entry('a'), entry('b')];
    expect(selectableModels(only).map((m) => m.name)).toEqual(['a', 'b']);
  });
});
