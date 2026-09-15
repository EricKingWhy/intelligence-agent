/** Composer 模型选择器（T10 #103 → Phase 2a Radix Popover）——SSR 渲染契约。
 *  列表来自端点（零硬编码模型名）；端点缺席（models 空）→ 选择器降级隐藏。
 *  选择器现在由 ModelPicker（Radix DropdownMenu）渲染：
 *   - trigger 是 button.composer-model，显示当前选中 label；
 *   - 选中 null 显示「默认链」，选中模型显示模型名；
 *  DropdownMenu 的弹出内容（具体模型条目）由 Radix portal 客户端渲染，不出现在 SSR。 */

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

describe('Composer 模型选择器（#103 / Phase 2a）', () => {
  it('端点缺席（models 空）：选择器不渲染——不伪造列表', () => {
    const html = renderToString(createElement(Composer, { ...base, models: [], selectedModel: null, onModelChange: noop }))
      .replaceAll('<!-- -->', '');
    expect(html).not.toContain('composer-model');
    expect(html).not.toContain('model-picker');
    expect(html).toContain('composer'); // 输入框照常在场
  });

  it('目录在场：trigger 渲染 + 显示「默认链」当前选中（null 选中）', () => {
    const html = renderToString(createElement(Composer, { ...base, models: CATALOG, selectedModel: null, onModelChange: noop }))
      .replaceAll('<!-- -->', '');
    expect(html).toContain('composer-model');
    expect(html).toContain('默认链');
    expect(html).toContain('aria-label="模型选择"');
  });

  it('selectedModel 有值 → trigger 显示该模型名', () => {
    const html = renderToString(
      createElement(Composer, { ...base, models: CATALOG, selectedModel: 'qwen-max', onModelChange: noop }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('composer-model');
    expect(html).toContain('qwen-max');
    // 不应再显示默认链 label（因为切到了具体模型）
    expect(html).not.toContain('>默认链<');
  });
});

describe('Composer 队列条（ADR-0030 §5.2）', () => {
  const queue = [
    { kind: 'queue' as const, id: 'q-1', content: '排队的问题', seq: 1, created_at: '' },
  ];

  it('空队列不渲染队列条（不占位不闪烁）', () => {
    const html = renderToString(createElement(Composer, { ...base, undelivered: [] }))
      .replaceAll('<!-- -->', '');
    expect(html).not.toContain('queue-bar');
  });

  it('有排队项 → 渲染条目 + 三个中文 aria 动作（编辑/立即/取消）', () => {
    const html = renderToString(
      createElement(Composer, {
        ...base, undelivered: queue,
        onEditItem: noop, onSteerItem: noop, onCancelItem: noop,
      }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('queue-bar');
    expect(html).toContain('排队的问题');
    expect(html).toContain('aria-label="编辑排队消息"');
    expect(html).toContain('aria-label="立即发送"');
    expect(html).toContain('aria-label="取消排队消息"');
  });

  it('steer 项不渲染三个动作（无后端取消/编辑通道，点了就是静默 404）', () => {
    const html = renderToString(
      createElement(Composer, {
        ...base,
        undelivered: [{ kind: 'steer' as const, id: 's-1', content: '引导的问题', seq: 2, created_at: '' }],
        onEditItem: noop, onSteerItem: noop, onCancelItem: noop,
      }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('引导的问题');
    expect(html).toContain('引导'); // 徽标仍在（状态可见）
    expect(html).not.toContain('aria-label="编辑排队消息"');
    expect(html).not.toContain('aria-label="取消排队消息"');
  });
});
