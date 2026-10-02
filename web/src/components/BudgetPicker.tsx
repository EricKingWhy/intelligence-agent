/** BudgetPicker — #536：预算三项（turns / total_tokens / deadline_at）的**单入口**
 *  popover（替换 #426 的三平铺输入，设计稿 §2.3）。
 *
 *  结构：`BudgetPicker`（Radix Popover 壳，trigger 常显 `budgetSummary` 摘要）+
 *  `BudgetPickerPanel`（面板主体，独立导出——本仓没有 jsdom，Portal 内容 SSR 断不了，
 *  抽成纯 props 组件才能在 BudgetPicker.test.tsx 里直接渲染锁定）。
 *
 *  三维控件：
 *   - turns：数值输入（aria-label 沿用 #426 旧名，e2e 定位器资产不因换壳作废）；
 *   - tokens：OptionPicker 档位（§2.1：100k/250k/500k/1M/自定义——档位 value 即
 *     数字字符串，draft 原样进 toCreateBudget，**不做** token→次数换算）；
 *     「自定义」档选中后不关闭面板（onChange 返回 false），展开的数值输入框挂在
 *     OptionPicker 的 footer 插槽；
 *   - deadline：OptionPicker 时长档（§2.2：30m/1h/2h/4h/自定义时长）+ datetime-local
 *     高级路径输入（两形态共用一个 draft 字符串，天然互斥：regex 判形在
 *     `amend.parseDurationToken`，换算唯一执行点 `amend.resolveDeadlineDraft`）。
 *
 *  预览行（§2.2）：`deadlinePreview` 渲染「≈ MM-DD HH:mm 截止（2 小时后）」；
 *  过期 → 「已过期」+ `--danger`（仅前置提示，不影响提交——后端才是权威）。
 *  `now` 由壳层每次渲染传入（draft 变化即刷新；分钟级新鲜度足够，不引入定时器）。 */

import * as Popover from '@radix-ui/react-popover';
import { useState } from 'react';
import { ChevronDown, Coins, Timer, Wallet } from 'lucide-react';
import { OptionPicker } from './OptionPicker';
import { parseDurationToken } from '../lib/amend';
import {
  DURATION_CUSTOM_ID,
  DURATION_TIERS,
  TOKEN_CUSTOM_ID,
  TOKEN_TIERS,
  budgetSummary,
  deadlinePreview,
  durationTierId,
  tokensTierId,
} from '../lib/budgetUi';

export interface BudgetPickerPanelProps {
  turns: string;
  onTurnsChange?: (value: string) => void;
  tokens: string;
  onTokensChange?: (value: string) => void;
  deadline: string;
  onDeadlineChange?: (value: string) => void;
  disabled?: boolean;
  /** 预览/过期判定的「现在」。壳层传 `new Date()`；测试注入固定时刻。 */
  now: Date;
}

/** tokens 档位目录（含「自定义」哨兵行；value 即 draft 字符串）。 */
const TOKEN_OPTIONS = [
  ...TOKEN_TIERS.map((t) => ({ value: t.id, title: t.label })),
  { value: TOKEN_CUSTOM_ID, title: '自定义' },
];

/** deadline 时长档目录（含「自定义时长」哨兵行；档位 value 即时长 token）。 */
const DURATION_OPTIONS = [
  ...DURATION_TIERS.map((t) => ({ value: t.id, title: t.label })),
  { value: DURATION_CUSTOM_ID, title: '自定义时长' },
];

export function BudgetPickerPanel({
  turns,
  onTurnsChange,
  tokens,
  onTokensChange,
  deadline,
  onDeadlineChange,
  disabled = false,
  now,
}: BudgetPickerPanelProps) {
  // 「自定义」档展开态：draft 已是自定义值（重开面板）或本轮刚点了哨兵行。
  const [customTokensPicked, setCustomTokensPicked] = useState(false);
  const [customDurationPicked, setCustomDurationPicked] = useState(false);
  const tokensCustom = tokensTierId(tokens) === TOKEN_CUSTOM_ID || customTokensPicked;
  const durationCustom = durationTierId(deadline) === DURATION_CUSTOM_ID || customDurationPicked;
  // 预览只跟随被接线的维（面板里没有的那一维不产生预览行）。
  const preview = onDeadlineChange !== undefined ? deadlinePreview(deadline, now) : null;

  return (
    <>
      {onTurnsChange !== undefined && (
        <label className="composer-budget" title="本次 run 的 Agent turn 绝对上限；留空 = 后端默认。到顶自动暂停，可在恢复面板抬高后继续。">
          <span className="composer-budget-label">turns 上限</span>
          <input
            type="number"
            min={1}
            inputMode="numeric"
            className="composer-budget-input"
            value={turns}
            onChange={(e) => onTurnsChange(e.target.value)}
            placeholder="默认"
            disabled={disabled}
            aria-label="预算上限（Agent turns，留空为默认）"
          />
        </label>
      )}
      {onTokensChange !== undefined && (
        <div className="composer-budget">
          <OptionPicker
            ariaLabel="预算 tokens 档位"
            title="本次 run 的总 token 上限？"
            icon={Coins}
            options={TOKEN_OPTIONS}
            value={tokensTierId(tokens)}
            placeholder="tokens 上限"
            disabled={disabled}
            onChange={(value) => {
              if (value === null) {
                setCustomTokensPicked(false);
                onTokensChange('');
                return true;
              }
              if (value === TOKEN_CUSTOM_ID) {
                // 不关闭：自定义数值输入框就在 footer，先把面板留着。
                setCustomTokensPicked(true);
                return false;
              }
              setCustomTokensPicked(false);
              onTokensChange(value);
              return true;
            }}
            footer={
              tokensCustom ? (
                <label className="composer-budget" title="自定义总 token 绝对上限；留空 = 不设。">
                  <span className="composer-budget-label">自定义 tokens</span>
                  <input
                    type="number"
                    min={1}
                    inputMode="numeric"
                    className="composer-budget-input"
                    value={tokens}
                    onChange={(e) => onTokensChange(e.target.value)}
                    placeholder="默认"
                    disabled={disabled}
                    aria-label="预算上限（总 tokens，留空为默认）"
                  />
                </label>
              ) : undefined
            }
          />
        </div>
      )}
      {onDeadlineChange !== undefined && (
        <>
          <div className="composer-budget">
            <OptionPicker
              ariaLabel="预算截止时长档位"
              title="本次 run 距截止还有多久？"
              icon={Timer}
              options={DURATION_OPTIONS}
              value={durationTierId(deadline)}
              placeholder="截止时长"
              disabled={disabled}
              onChange={(value) => {
                if (value === null) {
                  setCustomDurationPicked(false);
                  onDeadlineChange('');
                  return true;
                }
                if (value === DURATION_CUSTOM_ID) {
                  setCustomDurationPicked(true);
                  return false;
                }
                setCustomDurationPicked(false);
                // 档位 id 即时长 token（'30m' / '2h'），draft 原样收。
                onDeadlineChange(value);
                return true;
              }}
              footer={
                durationCustom ? (
                  <label className="composer-budget" title="自定义时长（分钟），提交时换算为绝对截止时刻。">
                    <span className="composer-budget-label">自定义时长（分钟）</span>
                    <input
                      type="number"
                      min={1}
                      inputMode="numeric"
                      className="composer-budget-input"
                      value={String(parseDurationToken(deadline) ?? '')}
                      onChange={(e) => onDeadlineChange(e.target.value ? `${e.target.value}m` : '')}
                      placeholder="默认"
                      disabled={disabled}
                      aria-label="预算截止时长（分钟，自定义档）"
                    />
                  </label>
                ) : undefined
              }
            />
          </div>
          {/* 高级路径（§2.2 保留项）：绝对时刻直填。时长档激活时显示空——在
              datetime 里输入即覆盖时长 token（单 draft 字符串，天然互斥）。 */}
          <label className="composer-budget" title="高级：按本机时区直接指定绝对截止时刻（提交换算为 UTC）；与时长档二选一。">
            <span className="composer-budget-label">截止时刻（高级）</span>
            <input
              type="datetime-local"
              className="composer-budget-input composer-budget-input--datetime"
              value={durationTierId(deadline) !== null ? '' : deadline}
              onChange={(e) => onDeadlineChange(e.target.value)}
              disabled={disabled}
              aria-label="预算截止时间（留空为不设）"
            />
          </label>
          {preview && (
            <span
              className={`composer-budget-preview${preview.expired ? ' composer-budget-preview--danger' : ''}`}
            >
              {preview.text}
            </span>
          )}
        </>
      )}
    </>
  );
}

interface BudgetPickerProps {
  turns: string;
  onTurnsChange?: (value: string) => void;
  tokens: string;
  onTokensChange?: (value: string) => void;
  deadline: string;
  onDeadlineChange?: (value: string) => void;
  disabled?: boolean;
}

/** 单入口：trigger 常显摘要（§2.5——「默认」即全空，设了才展开对应部分）。 */
export function BudgetPicker({
  turns,
  onTurnsChange,
  tokens,
  onTokensChange,
  deadline,
  onDeadlineChange,
  disabled = false,
}: BudgetPickerProps) {
  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="composer-control composer-budget-trigger"
          aria-label="预算"
          title="本次 run 的预算（turns / tokens / 截止）；留空 = 后端默认。到顶/到点自动暂停，可在恢复面板调整后继续。"
          aria-disabled={disabled || undefined}
          disabled={disabled}
        >
          <Wallet size={13} className="composer-trigger-icon" aria-hidden="true" />
          <span className="composer-trigger-current">{budgetSummary(turns, tokens, deadline)}</span>
          <ChevronDown size={12} className="composer-trigger-chevron" aria-hidden="true" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="picker-content budget-picker-content" side="top" align="start" sideOffset={6}>
          <div className="picker-head">本次 run 的预算（留空 = 后端默认）</div>
          <BudgetPickerPanel
            turns={turns}
            onTurnsChange={onTurnsChange}
            tokens={tokens}
            onTokensChange={onTokensChange}
            deadline={deadline}
            onDeadlineChange={onDeadlineChange}
            disabled={disabled}
            now={new Date()}
          />
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
