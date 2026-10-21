/** Glyph / Caret 与 lucide 的**形状对账**（#1429 F2 的性能替身）。
 *
 * 这两个文件把每行都出现的图标换成手写的路径数据，理由是 lucide 组件每次渲染都要
 * 序列化 11 个默认属性（见 Glyph.tsx 文件头）。抄路径必须可验证：本文件直接渲染
 * 仓库里已安装的 lucide 组件，逐图标比对子元素与几何属性——形状不一致即红。
 *
 * 只比形状（tag / d / 圆与矩形的几何），不比属性全集：尺寸由宿主选择器给，绘制
 * （fill / stroke / 描边宽度）由 `app.css` 的 `.glyph` 规则给——那正是替换的目的。
 * 但「规则由 CSS 给」本身必须有守卫：`Glyph.test.tsx` 的另一条用例直接读 `app.css`，
 * 断言 `.glyph` 规则仍声明 `fill:none` / `stroke:currentColor` / `stroke-width:2`；
 * 规则被删或改坏 ⇒ 图标落回 SVG 默认的黑色实心，用例转红（#1429 复审 P1）。
 */

// node 内置模块没有声明（web 的 tsconfig 只带 vite/client）。
// @ts-expect-error node 内置模块没有声明
import { readFileSync } from 'node:fs';
// @ts-expect-error node 内置模块没有声明
import { cwd } from 'node:process';
import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { Check, ChevronDown, FilePen, Plug, Search, Square, Terminal, X } from 'lucide-react';
import { kindGlyph, statusGlyph } from './Glyph';
import { caret } from './Caret';

/** 从 svg 标记里抽子元素几何（顺序保留）。 */
function shapes(html: string): string[] {
  const out: string[] = [];
  for (const m of html.matchAll(/<(path|circle|rect)\b([^>]*)>/g)) {
    const attrs = m[2];
    const get = (name: string) => new RegExp(`\\b${name}="([^"]*)"`).exec(attrs)?.[1] ?? null;
    out.push(
      m[1] === 'path'
        ? `path ${get('d')}`
        : m[1] === 'circle'
          ? `circle ${get('cx')},${get('cy')},${get('r')}`
          : `rect ${get('width')}x${get('height')}@${get('x')},${get('y')} r=${get('rx')}`,
    );
  }
  return out;
}

describe('Glyph — 手写路径与 lucide 形状一致', () => {
  const cases: { name: string; ours: string; lucide: string }[] = [
    { name: 'success', ours: renderToStaticMarkup(statusGlyph('success')), lucide: renderToStaticMarkup(<Check size={24} />) },
    { name: 'failed', ours: renderToStaticMarkup(statusGlyph('failed')), lucide: renderToStaticMarkup(<X size={24} />) },
    { name: 'stopped', ours: renderToStaticMarkup(statusGlyph('stopped')), lucide: renderToStaticMarkup(<Square size={24} />) },
    { name: 'search', ours: renderToStaticMarkup(kindGlyph('search', 24)), lucide: renderToStaticMarkup(<Search size={24} />) },
    { name: 'terminal', ours: renderToStaticMarkup(kindGlyph('terminal', 24)), lucide: renderToStaticMarkup(<Terminal size={24} />) },
    { name: 'write', ours: renderToStaticMarkup(kindGlyph('write', 24)), lucide: renderToStaticMarkup(<FilePen size={24} />) },
    { name: 'mcp', ours: renderToStaticMarkup(kindGlyph('mcp', 24)), lucide: renderToStaticMarkup(<Plug size={24} />) },
    { name: 'caret', ours: renderToStaticMarkup(caret('act-chev')), lucide: renderToStaticMarkup(<ChevronDown size={24} />) },
  ];

  it.each(cases)('$name', ({ ours, lucide }) => {
    expect(shapes(ours)).toEqual(shapes(lucide));
  });

  it('不在高频表里的 kind 回退 lucide 组件（不是空图标）', () => {
    const html = renderToStaticMarkup(kindGlyph('tool', 14));
    expect(shapes(html).length).toBeGreaterThan(0);
  });
});

describe('Glyph — 绘制规则（fill/stroke）由 app.css 的 .glyph 规则提供', () => {
  const css = readFileSync(`${cwd()}/src/styles/app.css`, 'utf8');
  // 精确匹配 `.glyph { ... }`（不是 `.glyph-x` 之类前缀相同的类）。
  const rule = /\.glyph\s*\{([^}]*)\}/.exec(css)?.[1] ?? '';

  it('.glyph 规则存在', () => {
    expect(rule.trim()).not.toBe('');
  });

  it('声明 fill:none / stroke:currentColor / stroke-width:2（缺任一 ⇒ 图标回到 SVG 默认黑块）', () => {
    expect(rule).toMatch(/fill:\s*none/);
    expect(rule).toMatch(/stroke:\s*currentColor/);
    expect(rule).toMatch(/stroke-width:\s*2\b/);
  });
});
