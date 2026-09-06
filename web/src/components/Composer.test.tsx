/** Composer 模型选择器（T10 #103）——SSR 渲染契约，无需 testing-library。
 *  列表来自端点（零硬编码模型名）；端点缺席（models 空）→ 入口降级隐藏。 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import type { ModelCatalogEntry } from '../lib/api';
import { Composer } from './Composer';

const noop = () => {};
const base = { streaming: false, onSubmit: noop, onCancel: noop };

const CATALOG: ModelCatalogEntry[] = [
  { name: 'deepseek-v4-flash-0731', provider: 'senseaudio', model: 'deepseek-v4-flash-0731', default: true },
  { name: 'qwen-max', provider: 'senseaudio', model: 'qwen3.8-max-0902', default: false },
];

describe('Composer 模型选择器（#103）', () => {
  it('端点缺席（models 空）：选择器不渲染——不伪造列表', () => {
    const html = renderToString(createElement(Composer, { ...base, models: [], selectedModel: null, onModelChange: noop }))
      .replaceAll('<!-- -->', '');
    expect(html).not.toContain('composer-model');
    expect(html).toContain('composer'); // 输入框照常在场
  });

  it('目录在场：select 渲染目录条目 + 默认链空值选项（零硬编码模型名）', () => {
    const html = renderToString(createElement(Composer, { ...base, models: CATALOG, selectedModel: null, onModelChange: noop }))
      .replaceAll('<!-- -->', '');
    expect(html).toContain('composer-model');
    expect(html).toContain('deepseek-v4-flash-0731');
    expect(html).toContain('qwen-max');
    expect(html).toContain('默认链');
    expect(html).toContain('aria-label="模型选择"');
  });

  it('selectedModel 反映到 selected 选项', () => {
    const html = renderToString(
      createElement(Composer, { ...base, models: CATALOG, selectedModel: 'qwen-max', onModelChange: noop }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('value="qwen-max" selected');
  });
});
