/** 上下文满了一句话兜底弹窗（`#1197` 项 2）。
 *
 * 票面：水位到线**先自动压**；压不住（前面没得再压）才弹这一屏。它只说两件事——
 * 「上下文已经满了」和「换个窗口更大的模型继续吧」，加两个按钮：换模型 / 结束。
 *
 * 为什么是一个**独立组件**而不是 `PausedPanel` 的第五个分支：`#968` 的 stuck 暂停
 * 已经立过这条先例——一种暂停如果恢复动作不同（stuck 不能一键重试，context 抬任何
 * ceiling 都没用），就给它自己的引导面板。`PausedPanel` 的存在理由是"把四维读数摊开
 * 让用户改数字"，context 暂停恰恰**没有数字可改**，把两者塞进同一个组件只能靠布尔
 * 分支互相遮蔽，最后两边都读不懂。
 *
 * 设计说明 02：「失败时只说人话 —— 不再展示预算、ceiling、水位等内部术语，只给原因
 * 和下一步。」所以这里**刻意**不 import `pauseFacts` / `pauseReasonLabel`：一旦
 * 接上那套口径，`预算到顶`、`预算版本`、`收口` 这些词会顺着 props 爬回屏幕。技术细节
 * 的归属地是 run 诊断日志（后端），不是这个对话框。
 *
 * 用 Radix `Dialog` 而不是手写浮层（同 `ConstraintResolutionDialog`）：模态的焦点陷阱、
 * Esc 关闭、`aria-modal` 是键盘用户的基本盘，手写一遍只会写漏。
 *
 * 纯展示组件：不 fetch、不改会话状态；两个回调由 App 决定做什么。
 *
 * ⚠ 两个动作的**诚实边界**（后端事实，不是 UI 选择）：
 *   - 「结束」：暂停 run 的 task 已经 done，`POST /cancel` 只会拿到 200
 *     `no_active_run`（`runmanager.get_active` 对 terminal/paused 返回 None），
 *     后端**没有** abandon / dismiss 端点。所以这个按钮的语义只能是"收起这一屏"，
 *     由 App 记账；不要在这里假装它能终止什么。
 *   - 「换模型」：`ModelPicker` 的开态是它自己的内部 state（`ModelPicker.tsx:77`），
 *     App 无法命令式打开它；App 侧的处理是打开模型选择器（DOM 级触发，照抄
 *     `focus-composer` 那条先例）。 */
import * as Dialog from '@radix-ui/react-dialog';
import { AlertTriangle } from 'lucide-react';

export interface ContextFullDialogProps {
  /** 弹窗开合（App 持有：暂停事实来自投影，收起是用户意图，两者不能互相覆盖）。 */
  open: boolean;
  /** 「结束」：收起这一屏（后端无 abandon 端点，见文件头）。 */
  onEndRun: () => void;
  /** 「换模型」：打开模型选择器。 */
  onChangeModel: () => void;
}

export function ContextFullDialog({ open, onEndRun, onChangeModel }: ContextFullDialogProps) {
  return (
    <Dialog.Root open={open}>
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        <Dialog.Content className="project-dialog context-full-dialog">
          <Dialog.Title className="project-dialog-title">
            <AlertTriangle size={14} aria-hidden="true" /> 记忆容量已经满了
          </Dialog.Title>
          <Dialog.Description className="project-dialog-desc">
            前面没有可以精简的内容，换一个记忆容量更大的模型继续吧。
          </Dialog.Description>
          <div className="project-dialog-actions">
            <button type="button" className="btn-ghost" onClick={onEndRun}>
              结束
            </button>
            <button type="button" className="btn-primary" onClick={onChangeModel}>
              换模型
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
