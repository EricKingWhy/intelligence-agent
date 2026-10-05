/** ApprovalCard — inline interactive approval card (#37, PRD §2.2).
 *
 * Renders when ConversationState.pending_approvals is non-empty.
 * User approves/denies → POST /api/sessions/{id}/approve →
 * permission/resolved event removes from pending queue.
 *
 * fail-closed: backend defaults to deny on timeout.
 * one-shot: same approval_id can only be resolved once (409 = already done).
 *
 * OBS-015 fix: the catch block used to flip the card to "approved/denied"
 * on ANY error — a dangerous false positive for security interactions.
 * Now: 409 (AlreadyResolvedError) → idempotent success, flip card;
 * 404 (ApprovalGoneError) → 只读失效态（重试无意义）;
 * other errors → keep card pending, show error message, allow retry.
 *
 * APR-01 fix（第十一轮真机）：`approval_queues` 是纯内存、run 终结即 GC，而
 * `tool/approval-requested` 永留 JSONL → 重启后这张卡**每次刷新都会重演**，
 * 点了只有 404、也没有关闭路径。现在投影层把「run 已终结仍 pending」的审批
 * 标 `stale`（`projection.ts::markPendingApprovalsStale`），卡片渲染只读失效态：
 * 标题「审批已失效」+ 禁用按钮 + 说明，且不挂全局快捷键（一次 Ctrl+Enter
 * 不该只换来一个 404）。"重新发起审批"需后端在 resume 时重放 approval，不在本票范围。
 *
 * UI-01 重塑（PRD §UI-01，用户决策 D4：①②③⑤；倒计时 ④ 缓做）：
 *  - 结构化参数呈现（R7）：path 置顶可复制、command/content 原文块、
 *    old+new → DiffBlock；剩余键才 JSON 兜底——不再直出转义串。
 *  - description/reason 渲染（后端下发但此前未渲染）。
 *  - role="alertdialog" + 挂载焦点（多卡只有第一张聚焦，防焦点打架）。
 *  - 键盘路径：Ctrl/⌘+Enter 批准、Ctrl/⌘+Backspace 拒绝（按钮上以 kbd
 *    标注）。Esc 明确不占——它是全局中断运行，语义冲突。
 *  - 材质回归实心卡（R4 Glass Only Floating）：去 glass 类与第三级辉光。
 *
 * #420 AC3 单一状态源：卡片**不自演**「已批准/已拒绝」翻转——那两个是后端
 * 事实（permission/resolved 事件），本地 state 硬编会在流断时与真相分叉
 * （R5-B4：本地说已批准、投影停在 pending，composer 永锁）。本卡只持有
 * **交互状态** submitted（"我的 POST 成功了"）与 busy/error；决策结果一律
 * 等投影说话——resolved 事件到达后审批离开 pending 队列，卡片由调用方
 * （内联位 / 模态）随投影卸载。 */
import { useEffect, useRef, useState } from 'react';
import { ShieldAlert, ShieldCheck, Check, X } from 'lucide-react';
import type { PendingApproval } from '../types';
import { postApproval, AlreadyResolvedError, ApprovalGoneError } from '../lib/api';
import { classifyPreviewArgs } from '../lib/approvalPreview';
import { modKey } from '../lib/platform';
import { CopyButton } from './CopyButton';
import { DiffBlock } from './DiffBlock';

interface Props {
  sessionId: string;
  approval: PendingApproval;
  /** 多卡并存时只有第一张（Conversation 传 index===0）自动聚焦。 */
  autoFocus?: boolean;
  /** 只读失效态：投影判定（run 已终结的孤儿）或后端实证（提交回 404）。
   *  由 App 统一计算并同时驱动 composer 解锁——卡内不留第二份真相。 */
  invalid?: boolean;
  /** 提交时后端回 404 → 上报 approval_id，让 App 记下这条审批已失效。 */
  onGone?: () => void;
  /** POST 成功（含 409 幂等成功）后上报——#420 AC2：调用方据此对账一次
   *  （resync），保证决策后事件有一条消费路径。404 不算决策成功，不走这里。 */
  onDecided?: () => void;
  /** #421：作为 Radix Dialog.Content 的内容渲染（模态承载，见 ApprovalModal）。
   *  dialog 语义（role="dialog" aria-modal="true"）由 Radix 的 Content 提供，
   *  卡片自带的 alertdialog/aria-modal 必须让位——同一棵树里两层"模态"声明
   *  会让读屏自相矛盾。视觉与交互（键盘快捷键 / 决策流）一字不变。 */
  modal?: boolean;
}

export function ApprovalCard({ sessionId, approval, autoFocus = false, invalid: invalidProp = false, onGone, onDecided, modal = false }: Props) {
  // 交互状态（不是决策真相）：POST 成功后置位，防重复提交 + 隐藏按钮；
  // 「批准了还是拒绝了」由事件流投影回答（见文件头 #420 AC3）。
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // #684 第三档：点「以后都允许」后展开粒度选择器（精确 / 命令级）。粒度是**用户
  // 显式再选一次**才提交——不在展开时就默认某一档（F21 显式授权）。
  const [policyOpen, setPolicyOpen] = useState(false);
  const cardRef = useRef<HTMLDivElement>(null);
  const invalid = invalidProp;
  // 第三档只在后端 requested 事件声明了 approve_policy 时才渲染（F21/F22：入口由
  // Runtime 决定，前端不自己发明可选决策；不可缓存的工具身份后端不加它）。
  const allowPolicy = approval.allowed_decisions?.includes('approve_policy') ?? false;

  // 挂载聚焦一次即可：决策后不抢回焦点（用户可能已在别处操作）。
  useEffect(() => {
    if (autoFocus) cardRef.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const decide = async (
    approved: boolean,
    decision?: string,
    granularity?: 'exact' | 'command',
  ) => {
    if (busy || invalid || submitted) return;
    setBusy(true);
    setError(null);
    try {
      // 默认路径保持 3 参数调用（旧调用点逐字不变）；只有第三档才透传决策+粒度。
      if (decision === undefined) {
        await postApproval(sessionId, approval.approval_id, approved);
      } else {
        await postApproval(sessionId, approval.approval_id, approved, decision, granularity);
      }
      setSubmitted(true);
      // 决策成功 ≠ 客户端已看到结果：上报调用方对账一次（#420 AC2——流活着
      // 时它是 no-op；give-up 落 viewing 后它就是唯一的消费路径）。
      onDecided?.();
    } catch (e) {
      if (e instanceof AlreadyResolvedError) {
        // 409 = another tab or retry already resolved it; treat as our intent succeeding.
        setSubmitted(true);
        onDecided?.();
      } else if (e instanceof ApprovalGoneError) {
        // 404 = 后端队列里没有这条审批（重启/run 终结已 GC）——重试无意义。
        // 失效事实上报给 App（它同时管着 composer 锁），本卡只读下来。
        onGone?.();
      } else {
        // Network failure / 5xx / etc.: decision did NOT reach backend.
        // Keep card pending so user can retry; show visible error.
        setError(e instanceof Error ? e.message : '审批请求失败');
      }
    } finally {
      setBusy(false);
    }
  };

  // 键盘路径（UI-01 ③）：仅可提交且非 busy 时挂在 document 上；
  // submitted/busy 变化即重挂/移除，决后快捷键失效。
  // 安全约束（U-1 review P1）：**只有 autoFocus 卡（第一张 pending 卡）挂全局
  // 监听**——否则 N 卡并存时一次 Ctrl+Enter 会向 N 个 approval_id 各发一 POST，
  // 等于一次按键批量批准多个危险操作。
  // 失效卡同样不挂：一次 Ctrl+Enter 只该得到 404，不该发送请求。
  useEffect(() => {
    if (!autoFocus || submitted || busy || invalid) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.repeat) return;
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        void decide(true);
      } else if ((e.ctrlKey || e.metaKey) && e.key === 'Backspace') {
        e.preventDefault();
        void decide(false);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
    // decide 闭包内的 busy 守卫由本 effect 的依赖（busy）保证新鲜。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoFocus, submitted, busy]);

  const preview = classifyPreviewArgs(approval.arguments_preview);
  const desc = approval.description || approval.reason || '';
  const titleId = `approval-title-${approval.approval_id}`;
  const descId = desc ? `approval-desc-${approval.approval_id}` : undefined;
  const hasStructured =
    preview.command !== undefined ||
    preview.diff !== undefined ||
    preview.content !== undefined ||
    Object.keys(preview.rest).length > 0;
  const mod = modKey();

  return (
    <div
      ref={cardRef}
      className={`approval-card${invalid ? ' invalid' : ''}${submitted ? ' submitted' : ''}`}
      role={modal ? undefined : 'alertdialog'}
      aria-modal={modal ? undefined : 'false'}
      aria-labelledby={titleId}
      aria-describedby={descId}
      tabIndex={-1}
    >
      <div className="approval-header">
        <ShieldAlert size={16} className="approval-icon" />
        <span className="approval-title" id={titleId}>
          {invalid && '审批已失效'}
          {!invalid && submitted && '决策已提交'}
          {!invalid && !submitted && '需要审批'}
        </span>
      </div>
      {desc && (
        <p className="approval-desc" id={descId}>
          {desc}
        </p>
      )}
      <div className="approval-tool">
        <code>{approval.tool_name}</code>
        {approval.permission && <span className="approval-chip">{approval.permission}</span>}
        {approval.policy && <span className="approval-chip">{approval.policy}</span>}
      </div>
      {preview.path && (
        <div className="approval-path">
          <code>{preview.path}</code>
          <CopyButton text={preview.path} label="复制路径" />
        </div>
      )}
      {hasStructured && (
        <div className="approval-preview">
          {preview.command && (
            <pre className="approval-cmd">
              <span className="approval-cmd-prompt">$ </span>
              {preview.command}
            </pre>
          )}
          {preview.diff && <DiffBlock diff={preview.diff} />}
          {preview.content && <pre className="approval-content">{preview.content}</pre>}
          {Object.keys(preview.rest).length > 0 && (
            <pre className="approval-args">{JSON.stringify(preview.rest, null, 2)}</pre>
          )}
        </div>
      )}
      {/* #460：失效说明按「是否已提交」分支——已提交的用户已被说过「无法再提交」
          会失真（他确实提交过）；也不得回到 #444 修掉的「等待后端确认」措辞
          （该审批已死，没有等待对象）。 */}
      {invalid && (
        <p className="approval-invalid-note" role="status">
          {submitted
            ? '决策已提交——该审批随后失效，最终结果以事件流为准。'
            : '该审批已失效——所在运行已结束或服务已重启，决策无法再提交。'}
        </p>
      )}
      {error && !invalid && (
        <div className="approval-error" role="alert">
          {error}
        </div>
      )}
      {!submitted && (
        <div className="approval-actions">
          <button
            className="btn-primary approval-approve"
            disabled={busy || invalid}
            onClick={() => decide(true)}
          >
            <Check size={14} /> 批准 {!invalid && <kbd className="approval-kbd">{mod}+⏎</kbd>}
          </button>
          <button
            className="btn-ghost approval-deny"
            disabled={busy || invalid}
            onClick={() => decide(false)}
          >
            <X size={14} /> 拒绝 {!invalid && <kbd className="approval-kbd">{mod}+⌫</kbd>}
          </button>
          {allowPolicy && (
            <button
              className="btn-ghost approval-allow-policy"
              disabled={busy || invalid}
              aria-expanded={policyOpen}
              onClick={() => setPolicyOpen((open) => !open)}
            >
              <ShieldCheck size={14} /> 以后都允许
            </button>
          )}
        </div>
      )}
      {/* #684 第三档：粒度由用户显式再选一次（精确 / 命令级），不默认、不猜测
          （F22）；两个按钮都走同一 postApproval 透传 decision+policy_granularity。 */}
      {!submitted && allowPolicy && policyOpen && (
        <div
          className="approval-actions approval-policy-granularity"
          role="group"
          aria-label="持久授权粒度"
        >
          <button
            className="btn-ghost approval-policy-exact"
            disabled={busy || invalid}
            onClick={() => decide(true, 'approve_policy', 'exact')}
          >
            精确（含参数）
          </button>
          <button
            className="btn-ghost approval-policy-command"
            disabled={busy || invalid}
            onClick={() => decide(true, 'approve_policy', 'command')}
          >
            命令级（不含参数）
          </button>
        </div>
      )}
      {/* #444/#460：invalid 时本篇「等待后端确认」式措辞不渲染——submitted 的失效卡
          由上方失效说明的已提交分支承接（说结果以事件流为准，不说「等待」）。 */}
      {submitted && !invalid && (
        <p className="approval-invalid-note" role="status">
          决策已提交，等待后端确认——结果以事件流为准。
        </p>
      )}
    </div>
  );
}
