import { describe, expect, it } from 'vitest';
/* 组件源码走 Vite `?raw` 而不是 `node:fs`：app tsconfig 的 `types` 只有
 *  `["vite/client"]`，node 内置模块没有声明（同 `attachments.image-types.test.ts`）。 */
import source from './StepDetail.tsx?raw';
/* #1419：图标已无 DOM（盒 = `.timeline-row::before`，图形 = `.tl-dur::before`
 * 的 mask），行为测试无从反证；这里直接对样式源码设守卫。
 * CSS 不走 `?raw`：Vitest 默认把 `.css` 模块处理成空串（实测 length 0），
 * 读法照 `StepDetail.window.test.tsx` 的 node:fs 先例。 */
// @ts-expect-error node 内置模块没有声明
import { readFileSync } from 'node:fs';
// @ts-expect-error node 内置模块没有声明
import { dirname, join } from 'node:path';
// @ts-expect-error node 内置模块没有声明
import { fileURLToPath } from 'node:url';

const css = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '../styles/app.css'),
  'utf8',
);

/** P2-6：Timeline 的"只渲染窗口切片"与"展开子树不生成行"是结构约束，
 *  行为测试很难从 DOM 反证（内容仍在 DOM 里）。这里直接对组件源码设守卫：
 *  顺序错了（先 map 再 slice）或展开子树里塞了 `.timeline-row`，测试就红。 */

function section(from: string, to: string): string {
  const start = source.indexOf(from);
  expect(start, `missing ${from}`).toBeGreaterThan(-1);
  const end = source.indexOf(to, start + from.length);
  expect(end, `missing ${to}`).toBeGreaterThan(start);
  return source.slice(start, end);
}

describe('#1419 Timeline 结构守卫', () => {
  it('TimelineTab 先切窗口切片，再建 lane / map（不把全量行塞进 DOM）', () => {
    const tab = section('export function TimelineTab', 'const TimelineCard');
    const hidden = tab.indexOf('filtered.slice(hidden)');
    const lanes = tab.indexOf('buildLanes(visible');
    const map = tab.indexOf('items.map');
    expect(hidden).toBeGreaterThan(-1);
    expect(lanes).toBeGreaterThan(hidden);
    expect(map).toBeGreaterThan(lanes);
  });

  it('展开子树只挂 EventInspector，不生成第二层 .timeline-row', () => {
    const card = section('const TimelineCard', '// ── Changes tab');
    const expand = card.slice(card.indexOf('{expanded && ('));
    expect(expand).toContain('<EventInspector');
    expect(expand).not.toContain('timeline-row');
  });

  it('hover 命中卡片行身份属性（data-list-key），不是旧行的 data-tl-i', () => {
    const tab = section('export function TimelineTab', 'const TimelineCard');
    expect(tab).toContain("closest?.('[data-list-key]')");
    expect(tab).not.toContain('[data-tl-i]');
  });
});

/** 取一条 CSS 规则的声明块；选择器必须整行开头，避免命中注释里的同名文本。 */
function ruleBlock(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`^${escaped}\\s*\\{([^}]*)\\}`, 'm'));
  expect(match, `missing CSS rule ${selector}`).not.toBeNull();
  return match![1];
}

/** #1419：图标零 DOM 后，误删盒 / 图形 / 类别 data URI / fail 覆盖时
 *  行为测试与快照都发现不了；这组守卫直接读 `app.css` 源码。 */
describe('#1419 Timeline 图标 CSS 守卫', () => {
  it('图标盒是 20px 的 `.timeline-row::before`', () => {
    const box = ruleBlock('.timeline-row::before');
    expect(box).toMatch(/width:\s*20px/);
    expect(box).toMatch(/height:\s*20px/);
  });

  it('6 个类别选择器各自带 data URI 的 --tl-icon', () => {
    for (const cat of ['tool', 'model', 'agent', 'user', 'run', 'other']) {
      const block = ruleBlock(`.timeline-row[data-cat='${cat}']`);
      expect(block, `${cat} 缺少 --tl-icon`).toMatch(
        /--tl-icon:\s*url\(["']?data:image\/svg\+xml/,
      );
    }
  });

  it('图形层 `.tl-dur::before` 用 mask 引用 var(--tl-icon)', () => {
    const icon = ruleBlock('.tl-dur::before');
    expect(icon).toMatch(/(?:^|[;\s])mask(?:-image)?:\s*[^;]*var\(--tl-icon\)/);
  });

  it('fail 状态把 --tl-icon-color 覆盖为 --danger', () => {
    const fail = ruleBlock(".timeline-card[data-state='fail'] .timeline-row");
    expect(fail).toMatch(/--tl-icon-color:\s*var\(--danger\)/);
  });
});
