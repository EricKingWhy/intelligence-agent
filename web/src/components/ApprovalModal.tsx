/** ApprovalModal — 待决审批的模态承载（#421，R5-B3 的修法）。
 *
 *  **为什么要模态**：审批卡原先内联渲染在虚拟化轮次列表之后的静态流里，而列表行
 *  是 `position:absolute + translateY` 的绘制层——测高滞后时行会盖到卡片的按钮上
 *  （R5 真机取证：`elementFromPoint` 取到的是 turn 行，真实点击与 Playwright 点击
 *  都落空）。模态化把交互面从"流内静态节点"搬到 Radix 的 Portal + Overlay 上：
 *  - Overlay（fixed、z-index 50，`.palette-overlay` 同款）天然盖过一切虚拟化行；
 *  - Radix modal 语义挡掉外部指针事件——列表滚动/点击都不会打到卡片背后；
 *  - 焦点由 Radix 圈在浮层内，UI-01 的键盘闭环（Ctrl+Enter 批准 / Ctrl+⌫ 拒绝）
 *    照常工作（卡片 autoFocus 效果负责落焦，见 ApprovalCard）。
 *
 *  **没有决策不许关**（GitHub required-review 语义）：`open` 完全由候选审批的
 *  存在派生（controlled）；ESC / 点外部 / InteractOutside 一律 preventDefault，
 *  也不渲染任何 Close。唯一的关闭路径是后端确认——`permission/resolved` 事件把
 *  审批移出 pending 队列（或它被判失效，候选资格随之消失）。
 *
 *  **候选规则在调用方**（Conversation）：pending_approvals 里第一张**非失效**卡
 *  进模态；失效卡（stale / 404-gone）决策无法提交，模态化等于把用户锁死在一个
 *  只有禁用按钮的面板里——它们留在内联只读位（APR-01 语义不变）。多卡并存时
 *  第一张决完、事件清队后下一张顶上来；ApprovalCard 以 `key={approval_id}` 重挂，
 *  决策状态绝不跨审批泄漏。
 *
 *  Radix Dialog 仓内先例：DeleteSessionDialog（同款 Overlay / Portal 结构）。
 */
import * as Dialog from '@radix-ui/react-dialog';
import { ApprovalCard } from './ApprovalCard';
import type { PendingApproval } from '../types';

interface Props {
  sessionId: string;
  approval: PendingApproval;
  /** 透传给卡片：提交回 404 → App 记失效（候选资格消失 = 模态关闭）。 */
  onGone?: () => void;
  /** 透传给卡片：POST 成功 → 调用方对账（#420 AC2）。流活着时是 no-op。 */
  onDecided?: () => void;
}

export function ApprovalModal({ sessionId, approval, onGone, onDecided }: Props) {
  return (
    /* open 恒为 true：挂载即打开，关闭 = 卸载（由候选资格派生，见文件头）。
       onOpenChange 忽略一切关闭请求——那是"没有决策不许关"的实现点。 */
    <Dialog.Root open onOpenChange={() => {}}>
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        <Dialog.Content
          className="approval-modal-content"
          onEscapeKeyDown={(e) => e.preventDefault()}
          onPointerDownOutside={(e) => e.preventDefault()}
          onInteractOutside={(e) => e.preventDefault()}
          /* 初焦不交给 Radix（它会把焦点落给第一个可聚焦元素 = 批准按钮）：
             危险动作不该是回车的默认落点（与 DeleteSessionDialog 初焦落
             「取消」同一原则）。卡片的 autoFocus 效果会把焦点放到卡容器上。 */
          onOpenAutoFocus={(e) => e.preventDefault()}
        >
          {/* Radix 用 Title 命名 dialog；卡片头部已有可见标题，这里是读屏名。
              视觉隐藏样式见 `.approval-modal-title`。 */}
          <Dialog.Title className="approval-modal-title">需要审批</Dialog.Title>
          <ApprovalCard
            key={approval.approval_id}
            sessionId={sessionId}
            approval={approval}
            autoFocus
            onGone={onGone}
            onDecided={onDecided}
            modal
          />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
