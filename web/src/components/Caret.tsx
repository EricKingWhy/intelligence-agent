/** Caret — 折叠指示的 V 形描边（#1429 打磨点 2 的 chevron）。
 *
 * 形状与 lucide `ChevronDown` 完全相同（`m6 9 6 6 6-6`，2px 圆头描边），但**不带**
 * lucide 组件的 11 个默认属性。这不是手抄图标库：`<ChevronDown size={11}/>` 每次
 * 渲染都要建 svg + path 并序列化那串属性，200 行工具卡 + 67 个组头常驻时是单次
 * 渲染里可测的固定税（perf 探针：N=200 卡片 ≈ +3.5ms）。描边/尺寸/颜色全部由
 * CSS 给（`.act-chev` / `.act-group-chev`），元素本身零内联样式。
 */
/* 返回元素的函数（不是组件）：调用点 `{caret('act-chev')}`，省掉每行一次组件边界。 */
export function caret(className: string) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}
