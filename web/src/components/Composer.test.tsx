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

// ── #283（F18-B）：会话内可改权限档 + 中性禁用文案 ──
// pill 的**触发按钮**不在 Radix portal 里，所以它的禁用态与 title 是 SSR 可断的；浮层内的
// 升档确认面（role/aria + 取消不发请求）在 `e2e/control-row.spec.ts` 里锁——本仓没有 jsdom。

describe('Composer 权限 pill（#283）', () => {
  // 逐字对齐后端 PermissionPolicy 三档（`tooling/contract.py`），同 e2e fixtures 的口径。
  const MODES = [
    { id: 'read-only', display_name: '只读', description: '可读文件和运行只读工具，不可写入。', icon: 'lock' },
    { id: 'workspace-write', display_name: '工作区写入', description: '可读写工作区内文件；高危工具仍需审批。', icon: 'pencil' },
    { id: 'danger-full-access', display_name: '完全访问', description: '所有工具无需审批，含网络与系统副作用。仅在可信环境使用。', icon: 'unlock' },
  ];

  it('会话内且不忙 → pill **可交互**（#236 的「会话内一律只读」已随本票删除）', () => {
    const html = renderToString(createElement(Composer, {
      ...base, permissionModes: MODES, selectedPermissionMode: 'read-only',
      onPermissionModeChange: noop, permissionInSession: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-label="权限模式"');
    expect(html).not.toContain('aria-disabled="true"');
    // 假提示必须消失：改档可行之后「权限档在会话创建时确定，会话内不可修改」就是假话。
    expect(html).not.toContain('权限档在会话创建时确定，会话内不可修改');
  });

  it('AC4：本轮进行中 → pill 仍禁用，且原因是**中性**的（不再宣称档位不可变）', () => {
    const html = renderToString(createElement(Composer, {
      ...base, streaming: true, permissionModes: MODES, selectedPermissionMode: 'read-only',
      onPermissionModeChange: noop, permissionInSession: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain('title="本轮进行中，等这一轮结束再改档"');
    expect(html).not.toContain('权限档在会话创建时确定，会话内不可修改');
  });

  it('AC4：等待审批决策 → pill 仍禁用，且原因是**中性**的', () => {
    const html = renderToString(createElement(Composer, {
      ...base, approvalPending: true, permissionModes: MODES, selectedPermissionMode: 'read-only',
      onPermissionModeChange: noop, permissionInSession: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain('title="等待审批决策后再改档"');
  });

  it('新会话（permissionInSession 假）→ 仍是创建期选择，无会话内的锁', () => {
    const html = renderToString(createElement(Composer, {
      ...base, permissionModes: MODES, selectedPermissionMode: null, onPermissionModeChange: noop,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-label="权限模式"');
    expect(html).toContain('>权限<'); // 未选 → placeholder
    expect(html).not.toContain('aria-disabled="true"');
  });
});
