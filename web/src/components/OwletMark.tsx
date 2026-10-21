/** Owlet 品牌 mark——A 版（`inspector-redesign/v4-a.html`）的猫头鹰 SVG。
 *
 * 顶栏与空态共用同一图形，避免两处各画一只。眼睛/喙用 `--brand-mark-hole` 取所在
 * 容器的底色（A 版里是 `fill: var(--primary)`，即容器本色 ⇒ 视觉上是「挖空」）；
 * 本仓两主题的容器底色不同，由 `.appbar-logo` / `.empty-logo` 各自给出，
 * 不写死色值：#533afd 在亮色主题同时是容器渐变与 `currentColor`，直接复用会看不见。
 */
export function OwletMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className} aria-hidden="true" focusable="false">
      <path
        fill="currentColor"
        d="M12 3.4c-1.5 0-2.8.6-3.8 1.5L5.6 3.2l-.8 2.9C3.7 7.5 3 9.4 3 11.5V15c0 3.6 4 6.4 9 6.4s9-2.8 9-6.4v-3.5c0-4.3-3.6-8.1-9-8.1Z"
      />
      <circle cx="8.6" cy="10.6" r="2.1" fill="var(--brand-mark-hole, var(--surface-1))" />
      <circle cx="15.4" cy="10.6" r="2.1" fill="var(--brand-mark-hole, var(--surface-1))" />
      <path d="m12 13.2 1.3 2h-2.6l1.3-2Z" fill="var(--brand-mark-hole, var(--surface-1))" />
    </svg>
  );
}
