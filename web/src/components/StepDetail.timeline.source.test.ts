import { describe, expect, it } from 'vitest';
/* Vite `?raw` 而不是 `node:fs`：app tsconfig 的 `types` 只有 `["vite/client"]`，
 *  node 内置模块没有声明（同 `attachments.image-types.test.ts`）。 */
import source from './StepDetail.tsx?raw';

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
