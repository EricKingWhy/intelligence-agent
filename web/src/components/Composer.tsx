/** Composer — task input at the bottom of the conversation column.
 *
 * Submits on Cmd/Ctrl+Enter; Enter = queue mode, Cmd/Ctrl+Enter = steer (ADR-0030
 * §5.1 D10：streaming 不再禁用输入——「本页有活流」≠「服务端有在途 run」，
 * 两者混用正是 issue #196 的根因）。等待审批时仍禁用（与审批 UI 竞争）。
 * presetTask: 外部注入的示例任务（空状态 chip 点击），注入后仍可自由编辑。
 */

import { memo, useEffect, useState, type KeyboardEvent } from 'react';
import { ArrowUp, Brain, Pencil, Play, Shield, Square, User, X, Zap } from 'lucide-react';
import type { PresetTask, UndeliveredInput } from '../types';
import { modKey } from '../lib/platform';
import type { CatalogEntry, ModelCatalogEntry } from '../lib/api';
import { ModelPicker } from './ModelPicker';
import { OptionPicker, toCatalogOptions } from './OptionPicker';

interface Props {
  streaming: boolean;
  /** UI-01（D4-⑤）：存在待决审批时锁住 composer——运行被阻塞，新任务
   *  与审批互斥，不允许两条修复路径同时开放（评审 Riley 红旗）。 */
  approvalPending?: boolean;
  onSubmit: (task: string) => void;
  /** steer 提交（ADR-0030 §5.1：同一份输入立即投递——Ctrl/Cmd+Enter）。
   *  缺席 = 回退到 onSubmit（queue），既有调用零改动。 */
  onSteer?: (task: string) => void;
  onCancel: () => void;
  presetTask?: PresetTask | null;
  /** T10 #103 模型目录（GET /api/models）：空 = 端点缺席/解析失败 → 选择器
   *  降级隐藏（不伪造列表）。条目即目录真相，零硬编码模型名。 */
  models?: ModelCatalogEntry[];
  /** 当前选中（null = 默认链，提交不带 model 字段）。 */
  selectedModel?: string | null;
  onModelChange?: (name: string | null) => void;
  // ── Phase 2b Composer control row（Ticket F1）──
  /** GET /api/permission-modes 清单。空 → 隐藏控件。 */
  permissionModes?: CatalogEntry[];
  selectedPermissionMode?: string | null;
  onPermissionModeChange?: (id: string | null) => void;
  /** GET /api/agent-profiles 清单。空 → 隐藏控件。 */
  agentProfiles?: CatalogEntry[];
  selectedAgentProfile?: string | null;
  onAgentProfileChange?: (id: string | null) => void;
  /** GET /api/reasoning-efforts 清单。空 → 隐藏控件。 */
  reasoningEfforts?: CatalogEntry[];
  selectedReasoningEffort?: string | null;
  onReasoningEffortChange?: (id: string | null) => void;
  // ── ADR-0030 §5.2 队列条（#195）──
  /** 未投递输入（事件流逐事件折叠，事件流是唯一事实）。空 → 队列条不渲染。 */
  undelivered?: UndeliveredInput[];
  /** 「立即」= 升级为 steer（POST /messages mode:steer，先取消原排队项）。 */
  onSteerItem?: (item: UndeliveredInput) => void;
  /** 「取消」= POST /sessions/{sid}/queue/{queue_id}/cancel。 */
  onCancelItem?: (item: UndeliveredInput) => void;
  /** 「编辑」= 就地编辑排队项（App 打开编辑态）。 */
  onEditItem?: (item: UndeliveredInput) => void;
  /** ADR-0030 §4.6「立即发送」：立刻投递队首待发送输入（POST /queue/flush）。
   *  缺席 = 不渲染按钮（重启后手动投递是 ADR 明确的前端职责）。 */
  onFlush?: () => void;
}
// memo：流式期间 props 稳定（streaming 布尔不变、回调由 App useCallback 固定），
// 输入框不随对话区每个 delta 重渲染。
export const Composer = memo(function Composer({
  streaming,
  approvalPending = false,
  onSubmit,
  onSteer,
  onCancel,
  presetTask,
  models = [],
  selectedModel = null,
  onModelChange,
  permissionModes = [],
  selectedPermissionMode = null,
  onPermissionModeChange,
  agentProfiles = [],
  selectedAgentProfile = null,
  onAgentProfileChange,
  reasoningEfforts = [],
  selectedReasoningEffort = null,
  onReasoningEffortChange,
  undelivered = [],
  onSteerItem,
  onCancelItem,
  onEditItem,
  onFlush,
}: Props) {
  const [value, setValue] = useState('');

  // ADR-0030 §5.1 D10：`locked` 拆开——approvalPending 仍禁用（等待审批时输入
  // 无意义且与审批 UI 竞争）；streaming **不再**禁用。issue #196 的根因正是
  // 「streaming 表示本页有活流」被当成「服务端有在途 run」：跨客户端场景下
  // 输入框可用、消息却走 queue 分支静默丢失。现在流式期间发消息 = 显式选择
  // queue（Enter）/ steer（Ctrl/Cmd+Enter），两条通道都有后端消费。
  const locked = approvalPending;
  // 锁定提示只表达「审批阻塞」这一种原因；纯 streaming 有自己的 affordances（停止键/Esc 提示）。
  const showLock = approvalPending && !streaming;

  // 外部示例任务注入（引用变化即触发；每次点击 chip 生成新对象）
  useEffect(() => {
    if (presetTask) setValue(presetTask.text);
  }, [presetTask]);

  const submit = (mode: 'queue' | 'steer') => {
    const trimmed = value.trim();
    if (!trimmed || locked) return;
    if (mode === 'steer' && onSteer) {
      onSteer(trimmed);
    } else {
      onSubmit(trimmed);
    }
    setValue('');
  };

  // §5.1 发送键语义（与上游一致）：Enter = queue（默认）；Ctrl/Cmd+Enter = steer。
  // IME composition 守卫（审查 P1）：中文输入法按 Enter 确认拼音时
  // `isComposing` 为真——此时不提交，否则半截拼音会被当成任务发出。
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit('steer');
      return;
    }
    // Enter（无修饰键）= queue。Shift+Enter 保留换行（真实用户习惯）。
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit('queue');
    }
  };

  // 控件行是否渲染——至少有一个非空目录时才显示 control row 容器
  const hasControls =
    models.length > 0 ||
    permissionModes.length > 0 ||
    agentProfiles.length > 0 ||
    reasoningEfforts.length > 0;

  return (
    <div className="composer-wrap">
      {/* §5.2 队列条：Composer 输入框**正上方**（不与 .composer-esc-hint 争左下角，
          那是 #194 的病灶）。空队列不渲染（不占位不闪烁）。 */}
      {undelivered.length > 0 && (
        <div className="queue-bar" role="list" aria-label="待发送消息">
          {undelivered.map((item) => (
            <div key={item.id} className="queue-item" role="listitem" data-queue-id={item.id}>
              <span className={`queue-item-badge queue-item-${item.kind}`}>
                {item.kind === 'steer' ? '引导' : '排队'}
              </span>
              <span className="queue-item-content" title={item.content}>
                {item.content.length > 40 ? `${item.content.slice(0, 40)}…` : item.content}
              </span>
              <span className="queue-item-actions">
                <button
                  className="queue-item-btn"
                  onClick={() => onEditItem?.(item)}
                  disabled={!onEditItem}
                  aria-label="编辑排队消息"
                  title="编辑这条排队消息"
                >
                  <Pencil size={13} />
                </button>
                <button
                  className="queue-item-btn"
                  onClick={() => onSteerItem?.(item)}
                  disabled={!onSteerItem}
                  aria-label="立即发送"
                  title="立即发送（注入当前运行）"
                >
                  <Zap size={13} />
                </button>
                <button
                  className="queue-item-btn"
                  onClick={() => onCancelItem?.(item)}
                  disabled={!onCancelItem}
                  aria-label="取消排队消息"
                  title="取消这条排队消息"
                >
                  <X size={13} />
                </button>
              </span>
            </div>
          ))}
          {/* §4.6「立即发送全部」：重启后/空闲时手动投递（POST /queue/flush）。
              仅 onFlush 在场时渲染——flush 会开新 run，streaming 时不给
              （在途 run 的 flush 是 409，交给逐项「立即」更合适）。 */}
          {onFlush && !streaming && (
            <button
              className="queue-item-btn queue-flush-btn"
              onClick={onFlush}
              aria-label="立即发送全部"
              title="立刻投递待发送输入（POST /queue/flush）"
            >
              <Play size={13} />
              立即发送全部
            </button>
          )}
        </div>
      )}
      <div className="composer-dock surface-floating">
        {/* UI-01：审批待决时给出锁定原因（置灰不是隐形）。 */}
        {showLock && <div className="composer-locked-hint">等待审批决策后再继续</div>}
        <textarea
          id="composer-input"
          name="task"
          className="composer"
          placeholder={showLock ? '等待审批决策…' : `描述一个任务…（${modKey()}+Enter 发送）`}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          rows={2}
          disabled={locked}
          aria-label="Agent 任务"
        />
        {hasControls && (
          <div className="composer-controls">
            <ModelPicker
              models={models}
              selectedModel={selectedModel}
              onModelChange={onModelChange ?? (() => {})}
              disabled={locked}
            />
            {/* #201：三个档位下拉合并为同一个 OptionPicker——同一份实现、同一份视觉、
                同一套 ARIA（此前三处手抄 + 十条不一致）。
                ⚠ `aria-label` **刻意维持原值的中英混用**（`权限模式` / `Agent Profile` /
                `Reasoning Effort`）：票面冻结结论 B 明确「会影响到 e2e 定位器就不统一中文」，
                并点名「不得内置默认值，否则合并本身就会顺手把英文名改掉，等于偷偷做了 B 项」。
                所以这里传的是各调用点原来的名字——顺手统一会连同 locator 一起改，
                正是那条冻结结论要防的事。要统一请先改票面结论。 */}
            <OptionPicker
              ariaLabel="权限模式"
              title="工具调用如何批准？"
              options={toCatalogOptions(permissionModes)}
              value={selectedPermissionMode}
              onChange={onPermissionModeChange ?? (() => {})}
              icon={Shield}
              placeholder="权限"
              disabled={locked}
            />
            <OptionPicker
              ariaLabel="Agent Profile"
              title="这次会话用哪个档位？"
              options={toCatalogOptions(agentProfiles)}
              value={selectedAgentProfile}
              onChange={onAgentProfileChange ?? (() => {})}
              icon={User}
              placeholder="Agent"
              disabled={locked}
            />
            <OptionPicker
              ariaLabel="Reasoning Effort"
              title="推理深度选哪一档？"
              options={toCatalogOptions(reasoningEfforts)}
              value={selectedReasoningEffort}
              onChange={onReasoningEffortChange ?? (() => {})}
              icon={Brain}
              placeholder="推理"
              disabled={locked}
            />
          </div>
        )}
        {/* #194：右侧动作簇——发送/停止按钮与 Esc 提示**同一处、同一行**。
            此前提示绝对定位在 dock 左下角，正好压在档位行（模型选择器）上：同一个
            动作的两个 affordance 被放在了相反的两侧。现在两者共用一个右锚点，按钮
            的像素位置与改动前完全一致（right/bottom 同一组值），提示作为它的左邻出现。 */}
        <div className="composer-actions">
          {streaming && (
            /* Esc 中断提示（Claude Code "esc to interrupt" 语言）：键位绑定在 App
               全局，这里只做可见性——所以整个簇 pointer-events: none，只有按钮可点。 */
            <span className="composer-esc-hint" aria-hidden="true">
              <kbd>Esc</kbd> 停止
            </span>
          )}
          {streaming ? (
            <>
              {/* §5.1 D10：停止按钮与发送按钮**并存**（streaming 不再禁用输入）。
                  发送键 = queue（下个 run 接力）；点击停止 = 既有取消行为不变。 */}
              <button
                className="composer-send"
                onClick={() => submit('queue')}
                disabled={locked || !value.trim()}
                aria-label="发送"
                title={`发送（Enter 排队 · ${modKey()}+Enter 立即）`}
              >
                <ArrowUp size={16} />
              </button>
              <button className="composer-stop" onClick={onCancel} aria-label="停止" title="停止">
                <Square size={14} />
              </button>
            </>
          ) : (
            <button
              className="composer-send"
              onClick={() => submit('queue')}
              disabled={locked || !value.trim()}
              aria-label="发送"
              title={`发送（Enter 排队 · ${modKey()}+Enter 立即）`}
            >
              <ArrowUp size={16} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
});
