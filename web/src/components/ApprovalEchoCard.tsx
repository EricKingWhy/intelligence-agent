/** #420 AC3：审批结果回显卡——待决卡决出结果后的只读落点。
 *
 *  与待决卡同视觉族（`.approval-card` 材质），但**没有按钮、没有输入**：决策与
 *  原因只来自投影 `approval_decisions`（`permission/resolved` 事件回流），本地
 *  不伪造结果（点按后本地只能声明"已提交"——那张卡上的措辞是"等待后端确认"，
 *  决出后由本卡接管显示）。措辞与语义色复用 `lib/permission`：绿 = 已批准是
 *  断言，未知决策一律 neutral，缺字段渲染 `—`（PRD §4 No fake values）。 */
import type { ApprovalDecision } from '../types';
import { decisionLabel, verdictTone } from '../lib/permission';

export function ApprovalEchoCard({ decision }: { decision: ApprovalDecision }) {
  const tone = verdictTone(decision.decision);
  return (
    /* role="status"：决出结果是对用户点按的异步回应，辅助技术应当播报。 */
    <div className={`approval-card approval-echo ${tone}`} role="status">
      <div className="approval-echo-head">
        <span className="approval-echo-title">审批已决：{decisionLabel(decision.decision)}</span>
        {decision.tool_name ? <code className="approval-echo-tool">{decision.tool_name}</code> : null}
      </div>
      {decision.reason ? <p className="approval-echo-reason">{decision.reason}</p> : null}
    </div>
  );
}
