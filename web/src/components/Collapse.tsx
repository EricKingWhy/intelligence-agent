/** Collapse — `grid-template-rows: 0fr → 1fr` 的展开/收起容器（#1429 打磨点 2）。
 *
 * 为什么不是测高：`grid-template-rows` 由浏览器自己插值，内容高度未知也能动，
 * 且不引第三方动画依赖（票面「不引依赖」红线）。
 *
 * 与既有密度契约的关系：收起完成后子树**卸载**——`balanced` 档 DOM 里仍然没有
 * 内联明细（ToolCard/ReasoningBlock 的 SSR 契约断言不破）。展开时先以 0fr 挂载、
 * 下一帧再切 1fr（同帧切换不会产生过渡），所以首帧不闪。
 *
 * 状态推进全部走「渲染期对齐 props」+ rAF / transitionend，不在 effect 体里同步
 * setState（`react(set-state-in-effect)`）：挂载/展开标记在渲染期按 `open` 收敛，
 * 动画帧与收尾分别由 rAF、`transitionend` 推进。
 *
 * 收起动画依赖 `transitionend`；`prefers-reduced-motion` 下全局把时长压到
 * 0.01ms（事件照常触发），仍以 `durationMs` 兜底定时器收尾——不依赖事件必达。
 */

import { useEffect, useRef, useState, type ReactNode } from 'react';

interface Props {
  open: boolean;
  /** 附加到网格容器的类名（`.reasoning-expanded` / `.act-detail-inline` 等既有 hook）。 */
  className?: string;
  /** 收起兜底时长（与 CSS `--dur-in` 对齐）。 */
  durationMs?: number;
  id?: string;
  children: ReactNode;
}

export function Collapse({ open, className, durationMs = 240, id, children }: Props) {
  const [mounted, setMounted] = useState(open);
  const [expanded, setExpanded] = useState(open);
  const ref = useRef<HTMLDivElement>(null);

  // 渲染期对齐（React 文档的「props 变化时调整 state」）：新展开先以 0fr 挂载，
  // 新收起先撤 `is-open` 让过渡跑起来。两个分支各自收敛，不会自激。
  if (open && !mounted) {
    setMounted(true);
    setExpanded(false);
  }
  if (!open && expanded) setExpanded(false);

  // 下一帧再切 1fr：同一帧内 0fr → 1fr 没有可插值的起点，浏览器不会产生过渡。
  useEffect(() => {
    if (!open || !mounted || expanded) return;
    const frame = requestAnimationFrame(() => setExpanded(true));
    return () => cancelAnimationFrame(frame);
  }, [open, mounted, expanded]);

  // 收起完成 → 卸载子树。收尾只由 transitionend / 兜底定时器推进。
  useEffect(() => {
    if (open || !mounted) return;
    const el = ref.current;
    if (!el) return;
    const finish = () => setMounted(false);
    const onEnd = (event: TransitionEvent) => {
      if (event.propertyName === 'grid-template-rows') finish();
    };
    el.addEventListener('transitionend', onEnd);
    const timer = window.setTimeout(finish, durationMs + 120);
    return () => {
      el.removeEventListener('transitionend', onEnd);
      window.clearTimeout(timer);
    };
  }, [open, mounted, durationMs]);

  if (!mounted) return null;
  return (
    <div
      ref={ref}
      id={id}
      className={`disc-body${expanded ? ' is-open' : ''}${className ? ` ${className}` : ''}`}
    >
      <div className="disc-in">{children}</div>
    </div>
  );
}
