/** Glyph — 工具行 / 组头里高频出现的小图标，直接渲染路径数据。
 *
 * 为什么不用 lucide 组件：`<Check size={14} />` 每次渲染都要建一个组件元素，再把它
 * 序列化成带 11 个默认属性的 `<svg>`（xmlns / fill / stroke / stroke-width /
 * linecap / linejoin / width / height / viewBox / class / aria-hidden）。中间对话区
 * 一次渲染几百个这样的图标时，这些属性占了输出 HTML 的近三分之一，也是单次渲染里
 * 可测的固定税（perf 探针：N=200 卡片 ≈ 5.6ms / 200 个图标）。
 *
 * 这里只收**每行都出现**的少数几个：状态三态（成功 / 失败 / 中断）与四类工具图标。
 * 路径数据原样取自仓库已安装的 lucide-react（`dist/esm/icons/*.mjs`），形状完全
 * 相同——元素本身零内联样式：**盒尺寸**由宿主给（`.act-status svg` 10px、
 * `.act-icon` / `.act-group-ico` 13px），**绘制规则**（`fill:none; stroke:currentColor;
 * stroke-width:2; round cap/join`）由 `app.css` 的 `.glyph` 规则给。`Glyph.test.tsx`
 * 逐图标比对 lucide 的几何形状，并断言 `.glyph` 绘制规则在 `app.css` 里仍然存在
 * （规则被删 ⇒ 图标落回 SVG 默认的黑色实心，测试必须转红）。
 */

import { KIND_ICON, type RuntimeEventKind } from '../lib/eventKind';

type GlyphNode = [tag: 'path' | 'circle' | 'rect', attrs: Record<string, string>];

/** 状态三态（成功 / 失败 / 中断）。running 由 CSS 圆点表达，不需要图标。 */
const STATUS_GLYPHS = {
  success: [['path', { d: 'M20 6 9 17l-5-5' }]],
  failed: [
    ['path', { d: 'M18 6 6 18' }],
    ['path', { d: 'm6 6 12 12' }],
  ],
  stopped: [['rect', { width: '18', height: '18', x: '3', y: '3', rx: '2' }]],
} satisfies Record<string, GlyphNode[]>;

export type StatusGlyphName = keyof typeof STATUS_GLYPHS;

/** 工具行高频 kind（其余 kind 仍走 lucide：它们不在每行出现，没有性能理由抄）。 */
const KIND_GLYPHS = {
  search: [
    ['path', { d: 'm21 21-4.34-4.34' }],
    ['circle', { cx: '11', cy: '11', r: '8' }],
  ],
  terminal: [
    ['path', { d: 'M12 19h8' }],
    ['path', { d: 'm4 17 6-6-6-6' }],
  ],
  write: [
    ['path', { d: 'M12.659 22H18a2 2 0 0 0 2-2V8a2.4 2.4 0 0 0-.706-1.706l-3.588-3.588A2.4 2.4 0 0 0 14 2H6a2 2 0 0 0-2 2v9.34' }],
    ['path', { d: 'M14 2v5a1 1 0 0 0 1 1h5' }],
    ['path', { d: 'M10.378 12.622a1 1 0 0 1 3 3.003L8.36 20.637a2 2 0 0 1-.854.506l-2.867.837a.5.5 0 0 1-.62-.62l.836-2.869a2 2 0 0 1 .506-.853z' }],
  ],
  mcp: [
    ['path', { d: 'M12 22v-5' }],
    ['path', { d: 'M15 8V2' }],
    ['path', { d: 'M17 8a1 1 0 0 1 1 1v4a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V9a1 1 0 0 1 1-1z' }],
    ['path', { d: 'M9 8V2' }],
  ],
} satisfies Record<string, GlyphNode[]>;

export type KindGlyphName = keyof typeof KIND_GLYPHS;

function render(nodes: readonly GlyphNode[], className: string) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      {nodes.map(([tag, attrs], i) =>
        tag === 'path'
          ? <path key={i} d={attrs.d} />
          : tag === 'circle'
            ? <circle key={i} cx={attrs.cx} cy={attrs.cy} r={attrs.r} />
            : <rect key={i} width={attrs.width} height={attrs.height} x={attrs.x} y={attrs.y} rx={attrs.rx} />,
      )}
    </svg>
  );
}

/* 导出的是**返回元素的函数**而不是组件：调用点直接 `{statusGlyph(s)}`，省掉每行
 * 一次组件 fiber（500 行 × 3 个图标 = 1500 个多余边界；perf 探针实测）。这些函数
 * 不取 hook，按普通函数调用是合法的。 */

/** 状态图标（12px 框内 10px 字形，尺寸由 `.act-status svg` 给）。 */
export function statusGlyph(status: StatusGlyphName) {
  return render(STATUS_GLYPHS[status], 'glyph');
}

/** 工具 kind 图标；不在高频表里的 kind 回退 lucide 组件（它们不在每行出现）。 */
export function kindGlyph(kind: RuntimeEventKind, size: number) {
  const nodes = KIND_GLYPHS[kind as KindGlyphName];
  if (nodes) return render(nodes, 'glyph');
  const Icon = KIND_ICON[kind];
  return <Icon size={size} />;
}
