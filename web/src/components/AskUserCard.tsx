/** #1204：`ask_user` 通用提问——对话流**内嵌卡片**（对标 dsh，视觉逐项对齐
 *  `docs/plans/ask-user/ask.html`）。
 *
 *  与 `ConstraintResolutionDialog` 的分工：那个是窄场景（长期约束冲突）的**模态弹窗**，
 *  这个是通用提问的**流内卡片**。两者共用同一条恢复通道（`resume_basis: 'user_input'`），
 *  但答案 wire 形态不同（`{choice}` vs `{questions:[{question, answer}]}`）——分流判据
 *  在 App（按 `USER_INPUT_REQUESTED.data.kind`）与投影（`pending_ask_user`）。
 *
 *  用户裁决（2026-10-16，覆盖票面「仅整卡跳过」）：
 *   - A1：单题可**留空**提交，并保留显式的「跳过此题」；整卡「跳过」提交全 null。
 *   - B2：卡片级 scope chip 保留（多题共用一个标签，取第 1 题的 `header`）；
 *     `header` 仍按 ≤12 存参，前端**不**为每题另加 chip。
 *
 *  纯展示 + 本地交互态：答案经 `onSubmit` 交回 App 走 `resumePausedRun`，本组件不
 *  直接发请求、不持有会话真相（同 ConstraintResolutionDialog 的取舍）。
 */
import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, PenLine, X } from 'lucide-react';
import type {
  AskUserAnswer,
  AskUserAnswerValue,
  PendingAskUserRequest,
} from '../types';

export interface AskUserCardProps {
  request: PendingAskUserRequest;
  /** 恢复在途——禁用提交（同 ConstraintResolutionDialog 的 `resuming`）。 */
  resuming?: boolean;
  /** 恢复失败原文（App 的会话错误）；null = 无错误。 */
  errorMessage?: string | null;
  /** 后端配置的超时档（分钟）；null = 未知——渲染不带数字的中性提示（零伪造）。 */
  timeoutMinutes?: number | null;
  onSubmit: (answer: AskUserAnswer) => void;
}

/** 一题的本地作答草稿（未落盘）。`null` = 尚未作答（提交时归一为 `answer: null`）。 */
type AnswerDraft =
  | { kind: 'option'; index: number }
  | { kind: 'custom'; text: string }
  | null;

function toAnswerValue(draft: AnswerDraft, labels: readonly string[]): AskUserAnswerValue | null {
  if (draft === null) return null;
  if (draft.kind === 'option') {
    const label = labels[draft.index];
    return label ? { label } : null;
  }
  // 自定义行**非空才算作答**（提交校验）：只点开不输入 = 该题留空。
  const text = draft.text.trim();
  return text ? { custom_text: text } : null;
}

export function AskUserCard({
  request,
  resuming = false,
  errorMessage = null,
  timeoutMinutes = null,
  onSubmit,
}: AskUserCardProps) {
  const questions = request.questions;
  const [drafts, setDrafts] = useState<AnswerDraft[]>(() => questions.map(() => null));
  const [index, setIndex] = useState(0);
  // 与 mockup 同：只能前进到「已达过」的题，跳回前面重选无压力（`›` 到此为止）。
  const [maxReached, setMaxReached] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const [closed, setClosed] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const lastIndex = questions.length - 1;
  const question = questions[index];
  const draft = drafts[index] ?? null;

  const setDraft = (at: number, value: AnswerDraft) => {
    setDrafts((prev) => prev.map((item, i) => (i === at ? value : item)));
  };
  // 当前题被作答 / 跳过即解锁下一页。
  const reachNext = (at: number) => {
    setMaxReached((prev) => (at === prev && at < lastIndex ? at + 1 : prev));
  };

  const chooseOption = (optionIndex: number) => {
    setDraft(index, { kind: 'option', index: optionIndex });
    reachNext(index);
  };
  const chooseCustom = () => {
    if (draft?.kind !== 'custom') setDraft(index, { kind: 'custom', text: '' });
  };
  const editCustom = (text: string) => {
    setDraft(index, { kind: 'custom', text });
    if (text.trim()) reachNext(index);
  };
  const skipQuestion = () => {
    setDraft(index, null);
    reachNext(index);
    if (index < lastIndex) setIndex(index + 1);
  };
  const skipCard = () => {
    onSubmit({
      request_id: request.request_id,
      questions: questions.map((item) => ({ question: item.question, answer: null })),
    });
  };
  const submit = () => {
    onSubmit({
      request_id: request.request_id,
      questions: questions.map((item, i) => ({
        question: item.question,
        answer: toAnswerValue(drafts[i] ?? null, item.options.map((o) => o.label)),
      })),
    });
  };

  // 展开自定义行即聚焦输入框（mockup 行为）。
  useEffect(() => {
    if (!collapsed && draft?.kind === 'custom') inputRef.current?.focus();
  }, [collapsed, draft?.kind]);

  const timeoutTip = timeoutMinutes === null
    ? '长时间未回答将自动继续'
    : `用户在 ${timeoutMinutes} 分钟内未回答将自动继续`;

  if (closed) {
    return (
      <div className="ask-user-block">
        <div className="ask-user-status" role="status" aria-live="polite">
          <span className="ask-user-pulse" aria-hidden="true" />
          <i className="ask-user-qico" aria-hidden="true">?</i>
          提问 · 等待回答
        </div>
        <div className="ask-user-closed">
          <span>提问卡片已收起</span>
          <button type="button" className="ask-user-btn" onClick={() => setClosed(false)}>
            展开提问
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="ask-user-block">
      <div className="ask-user-status" role="status" aria-live="polite">
        <span className="ask-user-pulse" aria-hidden="true" />
        <i className="ask-user-qico" aria-hidden="true">?</i>
        提问 · 等待回答
      </div>

      <section
        className={`ask-user-card${collapsed ? ' collapsed' : ''}`}
        aria-label="运行中提问"
      >
        <div className="ask-user-top">
          {/* B2：卡片级 scope chip（多题共用一个标签，取第 1 题的 header）。 */}
          <span className="ask-user-scope">{questions[0].header}</span>
          <div className="ask-user-tools">
            <button
              type="button"
              className="ask-user-icon-btn"
              aria-label={collapsed ? '展开' : '收起'}
              aria-expanded={!collapsed}
              onClick={() => setCollapsed((value) => !value)}
            >
              {collapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </button>
            <button
              type="button"
              className="ask-user-icon-btn"
              aria-label="关闭"
              onClick={() => setClosed(true)}
            >
              <X size={14} />
            </button>
          </div>
        </div>

        <div className="ask-user-body" hidden={collapsed}>
          <div className="ask-user-title">{question.question}</div>
          {question.options.map((option, optionIndex) => {
            const selected = draft?.kind === 'option' && draft.index === optionIndex;
            return (
              <button
                type="button"
                key={`${optionIndex}-${option.label}`}
                className={`ask-user-opt${selected ? ' selected' : ''}`}
                aria-pressed={selected}
                onClick={() => chooseOption(optionIndex)}
              >
                <span className="ask-user-num">{optionIndex + 1}</span>
                <span className="ask-user-opt-body">
                  <span className="ask-user-opt-label">
                    {option.label}
                    {question.recommended === optionIndex && (
                      <span className="ask-user-rec">推荐</span>
                    )}
                  </span>
                  {option.description && (
                    <span className="ask-user-opt-desc">{option.description}</span>
                  )}
                </span>
              </button>
            );
          })}

          <button
            type="button"
            className={`ask-user-custom-row${draft?.kind === 'custom' ? ' selected' : ''}`}
            aria-pressed={draft?.kind === 'custom'}
            onClick={chooseCustom}
          >
            <PenLine size={13} aria-hidden="true" />
            输入你的答案
          </button>
          {draft?.kind === 'custom' && (
            <textarea
              ref={inputRef}
              className="ask-user-custom-input"
              value={draft.text}
              onChange={(event) => editCustom(event.target.value)}
              maxLength={100_000}
              placeholder="写下你的决定…"
            />
          )}
        </div>

        <div className="ask-user-foot" hidden={collapsed}>
          <button
            type="button"
            className="ask-user-nav"
            aria-label="上一题"
            disabled={index === 0}
            onClick={() => setIndex(index - 1)}
          >
            ‹
          </button>
          <span className="ask-user-page">{`${index + 1} / ${questions.length}`}</span>
          <button
            type="button"
            className="ask-user-nav"
            aria-label="下一题"
            disabled={index >= maxReached}
            onClick={() => setIndex(index + 1)}
          >
            ›
          </button>
          <span className="ask-user-spacer" />
          <span className="ask-user-timeout-tip">{timeoutTip}</span>
          <button type="button" className="ask-user-btn ask-user-skip-question" onClick={skipQuestion}>
            跳过此题
          </button>
          <button type="button" className="ask-user-btn ask-user-skip" onClick={skipCard} disabled={resuming}>
            跳过
          </button>
          <button
            type="button"
            className="ask-user-btn primary ask-user-submit"
            onClick={submit}
            disabled={resuming}
          >
            提交
          </button>
        </div>

        {errorMessage && <div className="ask-user-error" role="alert">{errorMessage}</div>}
      </section>
    </div>
  );
}
