/** OptionPicker 契约测试（#201）—— SSR 渲染断言。
 *
 *  本仓没有 jsdom，组件测试只跑 `renderToString`（首帧），所以这里锁的是**触发按钮的
 *  契约**与**降级行为**，交互（键盘、选中、搜索）在 e2e 里锁：
 *  - 端点缺席（options 空）：控件不渲染——不伪造列表；
 *  - 目录在场：trigger 渲染 + `aria-label` 逐字在场（由调用方传入，组件不内置默认值）；
 *  - value 有值 → trigger 显示该条目标题；value 不在目录里 → 归一化为 placeholder；
 *  - disabled=true → trigger 标记 aria-disabled；
 *  - `toCatalogOptions` 是真目录 → 选项的唯一映射点（空 description 不产出占位文案）。
 *
 *  合并前这四类断言分散在 ControlPicker.test.tsx 与 ContextProviderPicker.test.tsx
 *  两份近乎相同的文件里；多选那份已随组件删除（计数/幽灵 id 那几条测试不再有对应
 *  实现——多选控件是 #200/#203 的职责，见设计稿 §4）。 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { Shield } from 'lucide-react';
import { OptionPicker, toCatalogOptions } from './OptionPicker';
import type { CatalogEntry } from '../lib/api';
import { catalogIcon } from '../lib/catalogIcons';

const ENTRIES: CatalogEntry[] = [
  { id: 'auto', display_name: 'Auto Approve', description: '自动批准工具调用' },
  { id: 'ask', display_name: 'Ask Each Time', description: '每次工具调用都询问' },
  { id: 'deny', display_name: 'Deny All', description: '' },
];

const render = (props: Partial<Parameters<typeof OptionPicker>[0]>) =>
  renderToString(
    createElement(OptionPicker, {
      ariaLabel: '权限模式',
      title: '工具调用如何批准？',
      icon: Shield,
      placeholder: '权限',
      options: [],
      value: null,
      onChange: () => {},
      ...props,
    }),
  ).replaceAll('<!-- -->', '');

describe('OptionPicker（Radix Popover 契约，#201）', () => {
  it('端点缺席（options 空）：控件不渲染——不伪造列表', () => {
    const html = render({ options: [] });
    expect(html).not.toContain('composer-control');
    expect(html).not.toContain('权限');
  });

  it('目录在场：trigger 渲染且 aria-label 逐字在场（由调用方传入）', () => {
    const html = render({ options: toCatalogOptions(ENTRIES) });
    expect(html).toContain('composer-control');
    expect(html).toContain('aria-label="权限模式"');
    // 未选中时显示 placeholder
    expect(html).toContain('权限');
  });

  it('value 有值 → trigger 显示该条目标题（不是 id）', () => {
    const html = render({ options: toCatalogOptions(ENTRIES), value: 'ask' });
    expect(html).toContain('Ask Each Time');
    expect(html).not.toContain('>权限<');
  });

  it('value 已不在目录里 → 归一化为 placeholder（死选中值不留成空白）', () => {
    const html = render({ options: toCatalogOptions(ENTRIES), value: 'removed-mode' });
    expect(html).toContain('>权限<');
    expect(html).not.toContain('removed-mode');
  });

  it('disabled=true → trigger 标记 aria-disabled', () => {
    // 只认 `aria-disabled="true"`：原先写成 `/aria-disabled="true"|disabled/`，而同一个 button
    // 本来就带原生 `disabled`——那个 `|disabled` 让这条断言在 aria-disabled 被删掉时照样绿，
    // 而 aria-disabled 正是它在守的东西（两轴 review 的 Standards 轴指出）。
    const html = render({ options: toCatalogOptions(ENTRIES), disabled: true });
    expect(html).toContain('aria-disabled="true"');
  });

  it('弹层内容不在 SSR HTML 中（Radix portal 是客户端渲染）', () => {
    const html = render({ options: toCatalogOptions(ENTRIES) });
    expect(html).not.toContain('Auto Approve');
    expect(html).not.toContain('工具调用如何批准？');
  });
});

describe('toCatalogOptions — 目录 → 选项的唯一映射点', () => {
  it('id/display_name/description 逐字段映射（前端零硬编码文案）', () => {
    expect(toCatalogOptions(ENTRIES)).toEqual([
      { value: 'auto', title: 'Auto Approve', description: '自动批准工具调用' },
      { value: 'ask', title: 'Ask Each Time', description: '每次工具调用都询问' },
      // 空 description → undefined（不渲染描述行，也不填占位文案）
      { value: 'deny', title: 'Deny All', description: undefined },
    ]);
  });

  it('iconOf 给了映射表 → 已知 id 带 icon，未知 id **不带**（槽由渲染层补，不编字形）', () => {
    // 真实 id（后端 PermissionPolicy 的三个值）都在 catalogIcons 表里
    const real: CatalogEntry[] = [
      { id: 'read-only', display_name: '只读', description: '' },
      { id: 'workspace-write', display_name: '工作区写入', description: '' },
    ];
    const withIcons = toCatalogOptions(real, catalogIcon);
    expect(withIcons[0].icon).toBeDefined();
    expect(withIcons[1].icon).toBeDefined();
    // 后端扩展出来的档位（夹具里的 mode-0…mode-5）：**不给字形**——给未知 id 编一个
    // 字形正是产品禁止的「编占位」；行仍然对齐，因为槽是渲染层恒渲染的。
    const unknown = toCatalogOptions([{ id: 'mode-7', display_name: 'M7', description: '' }], catalogIcon);
    expect(unknown[0].icon).toBeUndefined();
    expect('icon' in unknown[0]).toBe(false);
  });

  it('不传 iconOf → 一个 icon 都不产出（老调用点零改动）', () => {
    expect(toCatalogOptions(ENTRIES).every((o) => o.icon === undefined)).toBe(true);
  });

  it('空目录 → 空数组（调用方据此隐藏入口）', () => {
    expect(toCatalogOptions([])).toEqual([]);
  });
});
