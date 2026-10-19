/** Tip — 全站唯一的 radix tooltip 包装（#1372 改动 6）。
 *
 * 为什么在这里包一层而不是各处直接写 `@radix-ui/react-tooltip`：
 *  - 统一 `delayDuration` / sideOffset / collisionPadding，避免每个调用点拨一遍；
 *  - 自带 `Provider`：组件单测直接渲染 `TopBar` / `Composer` 时也有祖先
 *    （Radix Tooltip 要求 Provider；缺了会抛错）。Provider 不产生 DOM。
 *
 * 无障碍：`Tooltip.Trigger asChild` 把 `aria-describedby` 挂到真实触发器上
 * （radix 1.2.16 `dist/index.mjs:186` 仅在打开时设置），可见文字仍是可访问名——
 * 不把 tooltip 文本当 `aria-label`（WCAG 2.5.3 Label in Name）。
 */

import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import type { ReactElement, ReactNode } from 'react';

interface TipProps {
  content: ReactNode;
  children: ReactElement;
  side?: 'top' | 'right' | 'bottom' | 'left';
  align?: 'start' | 'center' | 'end';
}

export function Tip({ content, children, side = 'top', align = 'center' }: TipProps) {
  return (
    <TooltipPrimitive.Provider delayDuration={350} skipDelayDuration={120}>
      <TooltipPrimitive.Root>
        <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
        <TooltipPrimitive.Portal>
          <TooltipPrimitive.Content
            className="tip-content"
            side={side}
            align={align}
            sideOffset={6}
            collisionPadding={8}
          >
            {content}
            <TooltipPrimitive.Arrow className="tip-arrow" width={8} height={4} />
          </TooltipPrimitive.Content>
        </TooltipPrimitive.Portal>
      </TooltipPrimitive.Root>
    </TooltipPrimitive.Provider>
  );
}
