/** 待裁决的「技术详情」折叠层（#1030 缺陷③ / AC2）。
 *
 *  AC2 要求"界面上任何面向用户的文案不含 `POST /api/sessions/{id}/recover`、
 *  `tool_call_id`、`verdict ∈` 等协议词汇；技术详情只在折叠层出现"——**不是**"消失"。
 *  后端 `RecoveryConflict` 的 detail 是写给调用方的（内嵌端点与 verdict 枚举），
 *  但 `tool_call_id` 也正是手工结清的唯一依据，翻译掉就等于把排查依据藏起来。
 *  故两层：第一层人话（`adjudicationHeadline`），原文逐字进这里（K8s Object
 *  Conditions 的 reason/message 双层，ADAPT——见票面「方案依据」）。
 *
 *  抽成组件而不是在三处各写一遍 `<details>`（审查 P3-3）：此前 App 的横幅、
 *  App 的恢复冲突条、裁决面板各有一份逐字相同的 markup，且 `adjudicationTechDetail()`
 *  被调用两次（一次当守卫、一次当内容）——重复的 markup 迟早会漂移成三种行为。
 *  归一后只有一处需要维护，且判定只做一次。
 *
 *  `message` 为空/全空白时**整块不渲染**（不画一个点开是空的折叠层）。 */
import { adjudicationTechDetail } from '../lib/recovery';

interface Props {
  /** 后端原始 detail（`recoverState.message` 或 409 的 `RecoverError.message`）。 */
  message: string | null;
}

export function AdjudicationTechDetail({ message }: Props) {
  const detail = adjudicationTechDetail(message);
  if (detail === null) return null;
  return (
    <details className="adjudication-tech">
      <summary>技术详情</summary>
      <p className="adjudication-tech-body">{detail}</p>
    </details>
  );
}
