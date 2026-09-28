/** normalizeProviderModels（ADR-0032 §8.1）：表单行 → 请求体 models 的规整契约。
 *
 *  钉住的是数据丢失类回归：整表替换式 PUT 下，漏发 label 或漏发第二个模型
 *  都等于静默删掉用户已有配置（首版只编辑 models[0] 且丢 label，即此 bug）。 */

import { describe, expect, it } from 'vitest';
import { normalizeProviderModels } from './providerModels';

describe('normalizeProviderModels', () => {
  it('保留全部行（不是只取第一条）——首版只编辑 models[0] 会丢模型', () => {
    const out = normalizeProviderModels([
      { model_id: 'deepseek-chat', label: '' },
      { model_id: 'deepseek-reasoner', label: '' },
    ]);
    expect(out).toEqual([
      { model_id: 'deepseek-chat' },
      { model_id: 'deepseek-reasoner' },
    ]);
  });

  it('label 随行保留——整表替换下漏发等于删掉用户已有显示名', () => {
    const out = normalizeProviderModels([
      { model_id: 'gpt-x', label: '生产主力' },
    ]);
    expect(out).toEqual([{ model_id: 'gpt-x', label: '生产主力' }]);
  });

  it('trim 首尾空白；label 为空则不发送该键', () => {
    const out = normalizeProviderModels([
      { model_id: '  gpt-x  ', label: '   ' },
    ]);
    expect(out).toEqual([{ model_id: 'gpt-x' }]);
  });

  it('丢弃 model_id 为空的行（后端 models 非空校验会 422）', () => {
    const out = normalizeProviderModels([
      { model_id: '', label: '孤儿显示名' },
      { model_id: '   ', label: '' },
      { model_id: 'real-model', label: '' },
    ]);
    expect(out).toEqual([{ model_id: 'real-model' }]);
  });

  it('按 model_id 去重，保序且首见优先（标签取第一条）', () => {
    const out = normalizeProviderModels([
      { model_id: 'dup', label: '先到的' },
      { model_id: 'dup', label: '后到的' },
      { model_id: 'other', label: '' },
    ]);
    expect(out).toEqual([
      { model_id: 'dup', label: '先到的' },
      { model_id: 'other' },
    ]);
  });

  it('全空行 → 空数组（调用方据此让后端 422，而不是发一行空模型）', () => {
    expect(normalizeProviderModels([{ model_id: '', label: '' }])).toEqual([]);
  });
});
