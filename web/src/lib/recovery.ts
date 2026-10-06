/** #357 W-13 恢复 UI 的纯函数层（无 React / 无 DOM）。
 *
 *  设计权威：`docs/agents/357-research.md` §9（修订 A）——把 UNKNOWN 摆给用户的
 *  形态是「后果语言 + 三选一 + 默认选中最安全项 + 审计留痕」，**绝不是**开放式
 *  技术问答（§8.8-1：七家成熟产品无一这么做；Codex #50118 是小白的反例）。
 *
 *  边界（不变量 #22）：本文件不发明任何裁决事实。默认动作来自后端
 *  `pending_decisions[].default_action`（后端读工具 `replay_safe` 算出）；probe
 *  文案逐字来自后端 `probe`（工具既有 `ReconcileHint`）。前端只做**最小可读化**
 *  与组装 wire 载荷，不维护第二套工具注册表、不伪造「已查到/未查到」结论。
 */

import {
  RECOVER_DECISION_SOURCE_MAX,
  type InterruptedProgressVersion,
  type PendingDecision,
  type RecoverDecisionInput,
  type ReconcileVerdict,
} from './api';

/** 来源自陈上限，与 `api.ts` 的 wire 契约（后端 `max_length=2000`）同口径——
 *  两处必须相等，测试 `recovery.test.ts` 钉住这条等值，避免双份常量漂移。 */
export const RECOVERY_SOURCE_MAX = RECOVER_DECISION_SOURCE_MAX;

export interface DecisionOption {
  verdict: ReconcileVerdict;
  /** 单选按钮的主文案（后果语言）。 */
  label: string;
  /** 主文案下的补充说明（点明服务端行为，避免用户误以为是本地动作）。 */
  help: string;
}

/** 三选一（顺序固定：确认成功 → 安全重做 → 先跳过）。`CONFIRM_SUCCESS` 永远
 *  不是默认项（默认恒为后端给的 `RETRY`/`DEFER`，即 #14 安全侧），因此这里只是
 *  把用户**主动改选**时才可能用到的第三项列出来。
 *
 *  `RETRY` 的文案必须写明「服务端取消原调用、由模型安全重发起，永不盲跑」；
 *  `DEFER` 必须写明「保持待裁决，稍后可重裁」——两者都是产品合同（修订 A §9.4-3），
 *  不能简写成「重试」「跳过」。 */
export const DECISION_OPTIONS: readonly DecisionOption[] = [
  {
    verdict: 'CONFIRM_SUCCESS',
    label: '当作已生效，继续',
    help: '仅在你已亲眼确认结果、或查过外部系统后选择；会记下你的来源（可选）。',
  },
  {
    verdict: 'RETRY',
    label: '当作没生效，安全重做',
    help: '服务端会取消原调用，由模型安全重发起——永不盲跑原操作。',
  },
  {
    verdict: 'DEFER',
    label: '先跳过，稍后再说',
    help: '保持待裁决状态，稍后可以重新裁决；现在不启动任何重做。',
  },
];

/** 「当作已生效」的来源可选项（降低小白门槛，修订 A §9.4-3）。来源是用户自陈，
 *  不是自动验证——可选项 + 自定义输入，用户也可以留空。 */
export const SOURCE_PRESETS: readonly string[] = ['我亲眼看到结果了', '我查了外部系统'];

/** 工具名的**最小可读化**：snake_case → 空格分隔。刻意不维护别名表——那会变成
 *  第二套工具注册表（#22）。空名如实回落占位，不编造工具名。 */
export function humanizeToolName(name: string): string {
  const readable = name.replace(/_/g, ' ').trim();
  return readable === '' ? '未命名工具' : readable;
}

export interface ProbeFact {
  state: '已查到' | '未查到';
  /** 后端建议原文（`suggested_action`）；`未查到` 时为 null。 */
  detail: string | null;
}

/** probe 转述：只把后端 hint 摆出来，**不判断**「是否已生效」。
 *  `verifiable && suggested_action` 才有可核对的线索（「已查到」= 查到了**核对方式**，
 *  不是查到了结果）；否则如实「未查到」，绝不用猜测词填充。 */
export function probeFact(probe: PendingDecision['probe']): ProbeFact {
  if (probe.verifiable && probe.suggested_action) {
    return { state: '已查到', detail: probe.suggested_action };
  }
  return { state: '未查到', detail: null };
}

export interface DecisionDraft {
  verdict: ReconcileVerdict;
  /** 来源预设选项之一（或空）。 */
  sourceChoice: string;
  /** 自定义来源文本；非空时优先于预设。 */
  sourceCustom: string;
}

/** 单卡的初值：默认选中项逐字取自后端 `default_action`（不变量 #22）。
 *  来源留空——来源是用户自陈，不能替他预填。 */
export function initialDraft(decision: PendingDecision): DecisionDraft {
  return { verdict: decision.default_action, sourceChoice: '', sourceCustom: '' };
}

export function initialDrafts(decisions: readonly PendingDecision[]): Record<string, DecisionDraft> {
  const out: Record<string, DecisionDraft> = {};
  for (const decision of decisions) out[decision.tool_call_id] = initialDraft(decision);
  return out;
}

/** 组装 POST /recover 的 wire 裁决载荷。
 *
 *  `source` **只在 `CONFIRM_SUCCESS` 时采集**（其余裁决的「来源」没有语义，发了
 *  只会污染审计）；自定义文本优先于预设，逐字保留（只去首尾空白）。超过上限时
 *  **抛错**而不是截断——来源是审计留痕，静默截断等于伪造（§9.6 诚实原则）。
 *  某卡缺 draft 时回落该卡后端默认 verdict，不整批丢弃。 */
export function assembleDecisions(
  decisions: readonly PendingDecision[],
  drafts: Record<string, DecisionDraft>,
): RecoverDecisionInput[] {
  return decisions.map((decision) => {
    const draft = drafts[decision.tool_call_id] ?? initialDraft(decision);
    let source: string | undefined;
    if (draft.verdict === 'CONFIRM_SUCCESS') {
      const text = (draft.sourceCustom.trim() || draft.sourceChoice.trim());
      if (text.length > RECOVERY_SOURCE_MAX) {
        throw new Error(`裁决来源过长（${text.length} 字符，上限 ${RECOVERY_SOURCE_MAX}）`);
      }
      source = text === '' ? undefined : text;
    }
    return { tool_call_id: decision.tool_call_id, verdict: draft.verdict, source };
  });
}

/** 进度文件版本的诚实文案。可读 → 版本 + 事件序号；缺失/不可读/不匹配各自
 *  如实区分，**绝不写「最新」**（文件读不到就是读不到）。 */
export function formatProgressVersion(progress: InterruptedProgressVersion): string {
  if ('schema_version' in progress) {
    return `版本 ${progress.schema_version} · 进度事件 #${progress.source_event_seq}`;
  }
  if (progress.status === 'missing') return '未知（进度文件缺失）';
  if (progress.status === 'unreadable') return '未知（进度文件不可读）';
  return '未知（版本不匹配）';
}

/** 供 UI 上色/加标记：可读 = ok，其余 = unknown（unknown 不得被渲染成安全绿）。 */
export function progressTone(progress: InterruptedProgressVersion): 'ok' | 'unknown' {
  return 'schema_version' in progress ? 'ok' : 'unknown';
}
