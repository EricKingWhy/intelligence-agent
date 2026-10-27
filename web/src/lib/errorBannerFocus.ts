/** #1509/#1520（A11Y-5）：错误横幅关闭后的焦点归宿链。
 *
 * 关闭一条可关闭横幅不能把焦点丢回 `<body>`——读屏用户会瞬间失去语境，
 * 键盘用户会从 Tab 序开头重新找起。三级归宿（票面 #1520 AC2 的判定顺序）：
 *
 *  1. **触发该错误的控件**：仍在 DOM、未禁用且本身可聚焦时优先归还；
 *  2. 横幅在 DOM 中的**下一个**可聚焦元素（跳过横幅自身子树，含其关闭按钮
 *     ——那是刚被点过的、正在消失的元素）；
 *  3. 都没有 → **承载该错误的区域容器**（调用方常驻的槽位 div，
 *     `tabindex="-1"`，仅承接程序焦点，不进 Tab 序）。
 *
 * 第 3 级不能是横幅本身：关闭即卸载横幅，对已离场元素 `focus()` 是静默 no-op，
 * 焦点会真的落回 body。判定抽成纯函数：三级链与边界（禁用 / 离场 / 子树跳过）
 * 在 `errorBannerFocus.test.ts` 精确锁；App 只接线（捕获触发元素、调本函数、
 * `focus()`），并兜底断言 `document.activeElement !== body`。
 */
const FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  '[href]',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

/** 触发控件是否仍可承接焦点：在 DOM、未禁用、命中可聚焦选择器。 */
export function isFocusable(el: HTMLElement): boolean {
  return el.isConnected && !el.hasAttribute('disabled') && el.matches(FOCUSABLE_SELECTOR);
}

/** 关闭横幅时焦点应落到的元素（三级链）；三级都不可得（横幅与槽位都为 null）
 *  才返回 null，调用方自行兜底。 */
export function resolveDismissFocusTarget(
  banner: HTMLElement | null,
  region: HTMLElement | null,
  trigger: HTMLElement | null,
): HTMLElement | null {
  if (trigger !== null && isFocusable(trigger)) return trigger;
  if (banner === null) return region;
  const next = Array.from(document.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).find(
    (el) =>
      !banner.contains(el)
      && (banner.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
  );
  return next ?? region;
}
