/** #420 AC3 回显：审批决出结果后，对话流里那张卡的"结果落点"。
 *
 *  数据**全部**来自投影的 `approval_decisions`（`projectPermissionResolved` 在把
 *  请求移出待决队列的同时留痕）——回显消费投影，不本地伪造（AC3 的原始诉求：
 *  本地只能声明"已提交"，"已批准/已拒绝"必须由后端事件回流）。
 *
 *  锚定规则（为什么需要 seen 集合）：`approval_decisions` 是**全会话**累计的
 *  （JSONL 回放/刷新都会重建出全部历史决策），而回显是**本次观看的 live 交互
 *  产物**——历史上已决的审批不该在每次进入会话时都平铺一张结果卡（时间线
 *  PERMISSION 段、工具卡与终答案才是常驻真相）。所以只回显「本次挂载期间见过
 *  它挂起」的 approval_id：见过 → 决出 → 留结果卡；刷新/切换会话即归位为历史。
 *  副产品：失效卡（stale/孤儿）被 fail-closed 超时拒绝时同样留下一张"已拒绝 +
 *  原因"——#420 家族的"卡消失没下文"在回显层也有落点。 */

import type { ApprovalDecision, ConversationState } from '../types';

/** 把当前待决审批的 id 登记进 seen 集合（渲染期调用，幂等）。 */
export function notePendingApprovals(seen: Set<string>, state: ConversationState): void {
  for (const a of state.pending_approvals) seen.add(a.approval_id);
}

/** 取回显列表：本次观看期间见过挂起、且已有投影决策记录的审批。 */
export function collectApprovalEchoes(
  seen: ReadonlySet<string>,
  state: ConversationState,
): ApprovalDecision[] {
  return state.approval_decisions.filter((d) => seen.has(d.approval_id));
}
