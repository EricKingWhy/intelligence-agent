/** Glyph / Caret 与 lucide 的**形状对账**（#1429 F2 的性能替身）。
 *
 * 这两个文件把每行都出现的图标换成手写的路径数据，理由是 lucide 组件每次渲染都要
 * 序列化 11 个默认属性（见 Glyph.tsx 文件头）。抄路径必须可验证：本文件直接渲染
 * 仓库里已安装的 lucide 组件，逐图标比对子元素与几何属性——形状不一致即红。
 *
 * 只比形状（tag / d / 圆与矩形的几何），不比属性全集：尺寸、描边、颜色由 CSS 给，
 * 那正是替换的目的。
 */

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
