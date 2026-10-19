/** Tip — radix tooltip 的统一入口（#1372 改动 6）。
 *
 * 为什么在这里包一层而不是各处直接写 `@radix-ui/react-tooltip`：
 *  - 统一 `delayDuration` / sideOffset / collisionPadding，避免每个调用点拨一遍；
 *  - `TipProvider` 在应用根部只挂一次（`main.tsx`），`delayDuration` /
 *    `skipDelayDuration` 跨所有 tooltip 共享——指针从一个触发器移到另一个时
 *    不必再等一轮延迟。单独渲染组件（单测 / SSR）时没有祖先 Provider，
 *    `Tip` 会就地兜底挂一个，保证与 1.2.16 的「必须有 Provider」契约兼容。
 *
 * 例外（不是本约定的漏网之鱼）：`StepDetail` 的 `.tl-tooltip` 是时间线行内
 * **富内容**浮层（跟随指针、含结构化数据），与这里的单行说明不是同一交互，
 * 不迁移；`OptionPicker` 在禁用的原生控件上保留 `title` 作降级通道
 * （disabled 元素收不到指针事件，Radix tooltip 不会打开）。
 *
 * 无障碍：`Tooltip.Trigger asChild` 把 `aria-describedby` 挂到真实触发器上
 * （radix 1.2.16 `dist/index.mjs:186` 仅在打开时设置），可见文字仍是可访问名——
 * 不把 tooltip 文本当 `aria-label`（WCAG 2.5.3 Label in Name）。
 */

import * as TooltipPrimitive from '@radix-ui/react-tooltip';
import { createContext, useContext, type ReactElement, type ReactNode } from 'react';

/** 只回答「上面已经有 TipProvider 了吗」；不承载任何 tooltip 状态。 */
const TipScopeContext = createContext(false);

/** 应用根部挂一次：所有 `Tip` 共享打开/跳过延迟。 */
export function TipProvider({ children }: { children: ReactNode }) {
  return (
    <TooltipPrimitive.Provider delayDuration={350} skipDelayDuration={120}>
      <TipScopeContext.Provider value>{children}</TipScopeContext.Provider>
    </TooltipPrimitive.Provider>
  );
}

interface TipProps {
  content: ReactNode;
  children: ReactElement;
  side?: 'top' | 'right' | 'bottom' | 'left';
  align?: 'start' | 'center' | 'end';
}

export function Tip({ content, children, side = 'top', align = 'center' }: TipProps) {
  const inherited = useContext(TipScopeContext);
  const root = (
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
  );
  // 根部的 Provider 缺失时（单测直接渲染组件）就地兜底。
  return inherited ? root : <TipProvider>{root}</TipProvider>;
}
