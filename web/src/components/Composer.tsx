/** Composer — task input at the bottom of the conversation column.
 *
 * Submits on Cmd/Ctrl+Enter; Enter = queue mode, Cmd/Ctrl+Enter = steer (ADR-0030
 * §5.1 D10：streaming 不再禁用输入——「本页有活流」≠「服务端有在途 run」，
 * 两者混用正是 issue #196 的根因）。等待审批时仍禁用（与审批 UI 竞争）。
 * presetTask: 外部注入的示例任务（空状态 chip 点击），注入后仍可自由编辑。
 */

import { memo, useEffect, useId, useState, type KeyboardEvent } from 'react';
import { ArrowUp, Brain, Check, Pencil, Play, Shield, Square, TriangleAlert, User, X, Zap } from 'lucide-react';
import type { PresetTask, UndeliveredInput } from '../types';
import { modKey } from '../lib/platform';
import { toolScopeNote } from '../lib/agentProfileScope';
import { catalogIcon } from '../lib/catalogIcons';
import type { CatalogEntry, ModelCatalogEntry } from '../lib/api';
import { ModelPicker } from './ModelPicker';
import { OptionPicker, toCatalogOptions } from './OptionPicker';

/** #283：升到这一档要在 pill 浮层里给一次显式确认（ADR-0041 D1——后端**不加**强制标志位，
 *  确认是 UX 层的责任）。字面量与后端 `PermissionPolicy.DANGER_FULL_ACCESS` 同值：它同时
 *  「目录里的一个 id」与「危险判定」，所以不在前端另立第二张表（那样两处一漂移，
 *  「确认」就会挂在错的档位上——比不确认更糟）。 */
const DANGER_PERMISSION_MODE = 'danger-full-access';

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
  /** 改档提交口。**失败必须向上抛**：本组件把原因就地回显在权限浮层里，不另开错误通道。
   *  缺席 = 只读展示（与其余控件同款降级）。 */
  onPermissionModeChange?: (id: string | null) => Promise<void> | void;
  /** #283：这个 pill 当前作用于**已存在的会话**（`selectedId !== null`）。
   *
   *  会话内改档走 #282 的 `POST /api/sessions/{id}/permission`（**下一轮 run 生效**）——
   *  #236 的「权限档创建时确定、会话内不可修改」已被 F18-A 推翻，那句悬停提示随本票
   *  删除（它是「假话引导」：改档可行之后它就不成立了）。
   *  新会话（`false`）仍是创建期选择：随 create 请求发出，本地意图即真相。 */
  permissionInSession?: boolean;
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
  /** 「编辑」= 就地编辑该排队项（ADR-0030 §5.2）：提交后走
   *  `POST /messages {content, queue_id}`（后端取消旧项 + 按 mode 重新投递）。 */
  onEditItem?: (item: UndeliveredInput, newContent: string) => void;
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
  permissionInSession = false,
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
  // ADR-0030 §5.2 就地编辑态：正在编辑的排队项 id + 草稿内容。只存 id 不存整条
  // item——条目会随事件流增删，存 id 让渲染始终对账当前事实（不变量 #22）。
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState('');

  // ADR-0030 §5.1 D10：`locked` 拆开——approvalPending 仍禁用（等待审批时输入
  // 无意义且与审批 UI 竞争）；streaming **不再**禁用。issue #196 的根因正是
  // 「streaming 表示本页有活流」被当成「服务端有在途 run」：跨客户端场景下
  // 输入框可用、消息却走 queue 分支静默丢失。现在流式期间发消息 = 显式选择
  // queue（Enter）/ steer（Ctrl/Cmd+Enter），两条通道都有后端消费。
  const locked = approvalPending;
  // 锁定提示只表达「审批阻塞」这一种原因；纯 streaming 有自己的 affordances（停止键/Esc 提示）。
  const showLock = approvalPending && !streaming;

  // ── #283 权限 pill：会话内可改 + 升档确认 ──
  // 禁用只剩两个**操作性**原因（都不是「档位不可变」）：等审批（后端闸门会 409，
  // ADR-0041 D5）与本轮进行中（档位下一轮才生效，本轮改了 UI 无从交代结果）。
  // `#236` 的「会话内一律只读」那一条已随本票删除。
  const permissionLocked = locked || streaming;
  const permissionDisabledHint = locked
    ? '等待审批决策后再改档'
    : streaming
      ? '本轮进行中，等这一轮结束再改档'
      : undefined;

  // 三态互斥：待确认（`pendingDanger`）> 提交中（`permissionBusy`）> 失败原因。
  const [pendingDanger, setPendingDanger] = useState<string | null>(null);
  const [permissionBusy, setPermissionBusy] = useState(false);
  const [permissionError, setPermissionError] = useState<string | null>(null);
  const dangerDescId = useId();

  /** 提交改档。**浮层保持打开直到回执**——提前关掉浮层等于「点了没反应」，那正是本票要
   *  消灭的形态；失败就把后端 detail 原样留在原地（409 是「先裁决那条审批」这类可执行
   *  的话），成功才关。 */
  const commitPermission = (next: string | null, close: () => void) => {
    setPermissionError(null);
    setPermissionBusy(true);
    void Promise.resolve(onPermissionModeChange?.(next))
      .then(() => {
        setPendingDanger(null);
        setPermissionBusy(false);
        close();
      })
      .catch((e: unknown) => {
        setPermissionBusy(false);
        setPermissionError((e as Error).message);
      });
  };

  /** 权限选中回调。返回 `false` = 否决本次关闭（契约见 `OptionPicker` 的 `onChange`）。 */
  const handlePermissionSelect = (next: string | null, close: () => void): boolean => {
    setPermissionError(null);
    // 新会话：创建期选择，没有后端往返、也无所谓「确认」——立即关（既有行为零改动）。
    if (!permissionInSession) {
      onPermissionModeChange?.(next);
      return true;
    }
    // 会话内：**升**到完全访问要一次显式确认（取消即不发请求）；降档直接提交。
    // 「重复选当前档」不必再问一遍——它不改变任何东西。
    if (next === DANGER_PERMISSION_MODE && next !== selectedPermissionMode) {
      setPendingDanger(next);
      return false;
    }
    commitPermission(next, close);
    return false;
  };

  /** 权限浮层底部：确认面 / 失败原因 / 提交态 / 会话内生效时机披露，四者互斥。
   *  用渲染函数形态是因为「确认」之后必须由 picker 关掉自己（`close` 只在那边存在）。 */
  const renderPermissionFooter = ({ close }: { close: () => void }) => {
    if (pendingDanger !== null) {
      return (
        <div
          className="picker-confirm"
          role="alertdialog"
          aria-label="确认升级到完全访问"
          aria-describedby={dangerDescId}
        >
          <TriangleAlert size={12} aria-hidden="true" />
          <span id={dangerDescId}>
            完全访问：此后所有工具调用都不再需要审批，含网络与系统副作用。确认升级？
          </span>
          <button
            type="button"
            className="project-btn project-btn-danger"
            onClick={() => commitPermission(pendingDanger, close)}
            disabled={permissionBusy}
          >
            {permissionBusy ? '提交中…' : '升级'}
          </button>
          <button
            type="button"
            className="project-btn"
            onClick={() => setPendingDanger(null)}
            disabled={permissionBusy}
          >
            取消
          </button>
        </div>
      );
    }
    if (permissionError !== null) {
      return (
        <div className="picker-confirm" role="alert">
          <TriangleAlert size={12} aria-hidden="true" />
          <span>改档失败：{permissionError}</span>
          <button type="button" className="project-btn" onClick={() => setPermissionError(null)}>
            知道了
          </button>
        </div>
      );
    }
    if (permissionBusy) return <span>提交中…</span>;
    // ADR-0041 D4：改档**下一轮 run 生效** ⇒ UI 不得承诺「立即生效」，得把时机说清。
    return permissionInSession ? (
      <span>改档从下一轮 run 起生效（本轮不受影响）</span>
    ) : undefined;
  };

  // 外部示例任务注入（引用变化即触发；每次点击 chip 生成新对象）
  useEffect(() => {
    if (presetTask) setValue(presetTask.text);
  }, [presetTask]);

  const submit = (mode: 'queue' | 'steer') => {
    const trimmed = value.trim();
    if (!trimmed || locked) return;
    // steer 通道的**失败兜底不在这一层**（#219）：没有可打断的在途 run 时后端回 409
    // （session/service.py::SteerTargetNotFound），由交付层改投 queue 把消息送到
    // （见 useSession.sendFollowUp 的 steer 回退）。这里不能拿 `streaming` 当
    // 「服务端有在途 run」用——那正是 issue #196 的病灶（`streaming` 只表示
    // **本页有活流**，跨客户端时判反）。
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
              {editingId === item.id && item.kind === 'queue' ? (
                /* §5.2 就地编辑：该行原地换成输入框 + 保存/取消，不弹模态、
                   不回填主输入框（回填 + 取消原项会让"取消"变成不可逆的丢条目）。 */
                <>
                  <input
                    className="queue-item-edit"
                    value={editDraft}
                    autoFocus
                    aria-label="编辑排队消息内容"
                    onChange={(e) => setEditDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') {
                        e.preventDefault();
                        setEditingId(null);
                        return;
                      }
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        const trimmed = editDraft.trim();
                        if (!trimmed) return;
                        onEditItem?.(item, trimmed);
                        setEditingId(null);
                      }
                    }}
                  />
                  <span className="queue-item-actions">
                    <button
                      className="queue-item-btn"
                      onClick={() => {
                        const trimmed = editDraft.trim();
                        if (!trimmed) return;
                        onEditItem?.(item, trimmed);
                        setEditingId(null);
                      }}
                      aria-label="保存排队消息"
                      title="保存这一条"
                    >
                      <Check size={13} />
                    </button>
                    <button
                      className="queue-item-btn"
                      onClick={() => setEditingId(null)}
                      aria-label="取消编辑"
                      title="取消编辑（不改动这条）"
                    >
                      <X size={13} />
                    </button>
                  </span>
                </>
              ) : (
                <>
              <span className={`queue-item-badge queue-item-${item.kind}`}>
                {item.kind === 'steer' ? '引导' : '排队'}
              </span>
              <span className="queue-item-content" title={item.content}>
                {item.content.length > 40 ? `${item.content.slice(0, 40)}…` : item.content}
              </span>
              {/* 三个动作只给**排队项**（ADR-0030 §5.2 的原文就是"每个排队项显示…
                  编辑/立即/取消"）。steer 项没有对应后端通道：取消/编辑都以 queue_id
                  定位（`cancel_queue` 只认 kind=queue），渲染出来点了就是静默 404。
                  等 steer 撤回有后端语义时再补（本票不加半条通道）。 */}
              {item.kind === 'queue' && (
              <span className="queue-item-actions">
                <button
                  className="queue-item-btn"
                  onClick={() => {
                    setEditingId(item.id);
                    setEditDraft(item.content);
                  }}
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
              )}
                </>
              )}
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
            {/* #283：会话内**可改**——#236 的「创建时定、会话内不可修改」已被 F18-A 推翻。
                真值仍只来自投影（`session/started` / `permission/changed` 折叠出的
                `session_permission_mode`，由 App 传入）；新会话 → 仍是创建期选择（随 create
                请求发出）。升到「完全访问」要一次显式确认，确认面在本浮层的 footer 里
                （见 `renderPermissionFooter`）。 */}
            <OptionPicker
              ariaLabel="权限模式"
              title="工具调用如何批准？"
              options={toCatalogOptions(permissionModes, catalogIcon)}
              value={selectedPermissionMode}
              onChange={handlePermissionSelect}
              icon={Shield}
              placeholder="权限"
              // 禁用只剩操作性原因（审批待决 / 本轮进行中）——不再是「档位不可变」。
              disabled={permissionLocked}
              disabledHint={permissionDisabledHint}
              // 会话内没有「默认（未选）」这个目标：端点只接受三个具体档位，`null` 发过去
              // 必然 422 ⇒ 留着就是一个点下去必错的死胡同。
              showDefault={!permissionInSession}
              footer={renderPermissionFooter}
            />
            <OptionPicker
              ariaLabel="Agent Profile"
              title="这次会话用哪个档位？"
              options={toCatalogOptions(agentProfiles, catalogIcon)}
              value={selectedAgentProfile}
              onChange={onAgentProfileChange ?? (() => {})}
              icon={User}
              placeholder="Agent"
              disabled={locked}
              // #201 冻结 AC：档位收窄提示（用户裁定"要提示，但从简，不能突兀"）。
              // 文案组装在纯函数里（`lib/agentProfileScope.ts`，vitest 直测）；
              // 没被收窄 / 后端没给 tool_scope → 该函数返回 null → 不渲染 footer。
              // 按**当前生效档位**披露：未选时按后端默认档位（main），因为那才是
              // 这次会话真正会用的工具面。
              footer={
                (() => {
                  const note = toolScopeNote(agentProfiles, selectedAgentProfile);
                  if (!note) return undefined;
                  // `tabIndex=0` 让它可聚焦（键盘用户也能拿到提示）；`aria-label`
                  // **以可见文字开头**再接 tooltip 内容——直接用 tooltip 当可访问名
                  // 会违反 WCAG 2.5.3（Label in Name：可访问名须包含可见文字，
                  // 否则语音控制/switch 用户照可见文字说不中）。
                  return (
                    <span
                      tabIndex={0}
                      title={note.title}
                      aria-label={`${note.text}；${note.title}`}
                    >
                      {note.text}
                    </span>
                  );
                })()
              }
            />
            <OptionPicker
              ariaLabel="Reasoning Effort"
              title="推理深度选哪一档？"
              options={toCatalogOptions(reasoningEfforts, catalogIcon)}
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
