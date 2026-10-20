/** TopBar — App Bar of the single-frame shell (Phase 2, Brief §5).
 *
 * Left: product identity. Center: current session title + Run Pulse
 * (signature #1 — icon + color + text, never color-only). Right:
 * one density dropdown (shows the current tier) + the「更多工具」menu.
 *
 * #1396：右簇只保留 2 个可见动作——密度下拉与「更多」。记忆 / 看板 / 恢复 /
 * 主题与既有管理项都收进同一个菜单，入口零丢失；图标按钮静止态是 ghost
 * （透明、无边框），hover/focus 才出现 surface。
 */

import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { Brain, Check, ChevronDown, Columns3, Ellipsis, Gauge, History, KeyRound, Moon, PanelRight, ShieldCheck, SlidersHorizontal, Sun, Timer } from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { Theme } from '../lib/theme';
import { formatDateTime } from '../lib/format';
import type { SessionSummary } from '../types';
import { Tip } from './Tooltip';
import { OwletMark } from './OwletMark';
import { DENSITIES, type TraceDensity } from '../lib/density';
import { deriveRunPulse, shouldShowWaitHint, waitingHintText } from '../lib/runState';
import { BUDGET_BADGE_UNITS, budgetBadgeFacts } from '../lib/runBudget';
import { decodeJwtClaims, getToken, onTokenChange, setToken } from '../lib/auth';
import { awaitingApproval as hasLiveApproval, displayedSessionUsage } from '../lib/projection';
import type { ConversationState } from '../types';

interface Props {
  conversation: ConversationState | null;
  streaming: boolean;
  inspectorOpen: boolean;
  onToggleInspector: () => void;
  /** Trace Density 四档（冻结决策）——状态归 App，这里只渲染切换控件。 */
  density: TraceDensity;
  onDensityChange: (d: TraceDensity) => void;
  /** 主题状态归 App（Command Palette Toggle Theme 与本按钮共享同一状态源）。 */
  theme: Theme;
  onToggleTheme: () => void;
  /** 401 已发生（App 广播）——「更多工具」触发按钮加提示点，引导配置 token。 */
  authRequired: boolean;
  /** 打开记忆管理浮层（MEM-5 / #160）。低频管理动作，放在 App Bar 右簇。 */
  onOpenMemories: () => void;
  /** 打开持久审批规则管理浮层（#684 Phase 2）。与记忆管理同簇的低频管理动作。 */
  onOpenApprovePolicy: () => void;
  /** 打开恢复列表浮层（#357 W-13）：列出上次运行留下的中断会话。可选——
   *  未提供时不渲染入口（保持 TopBar 既有测试夹具零迁移）。 */
  onOpenRecoveryList?: () => void;
  /** 打开 Agent 看板浮层（#1304）：会话四态投影。可选——未提供时不渲染入口
   *  （同 `onOpenRecoveryList` 的先例：既有测试夹具零迁移）。 */
  onOpenKanban?: () => void;
  /** #1296：打开自动值班独立面板。可选，保持既有 TopBar 测试夹具兼容。 */
  onOpenWatchtower?: () => void;
  /** #200：会话 id（context-usage 看板的数据源）；null = 无会话（按钮不渲染）。 */
  sessionId: string | null;
  /** 打开上下文容量看板（#200，设计稿 §5）。 */
  onOpenContextUsage: (sessionId: string) => void;
  /** #1197 项 5B：打开「上下文窗口」挡位选择器。可选——未提供时不渲染入口
   *  （同 `onOpenRecoveryList` 的先例：既有测试夹具零迁移）。 */
  onOpenContextWindow?: (sessionId: string) => void;
  /** #1030：后端待裁决清单非空（`GET /api/sessions/{id}/recovery` 或 409 载荷）。
   *  可选 + 缺省 false——既有测试夹具零迁移（同 `onOpenRecoveryList` 的先例）。
   *  为真时徽标显示一等态「待裁决」而非「思考中」：坏死 run 的 `run_status` 停在
   *  'running'，只看事件流必然推出「思考中」（本票缺陷①）。 */
  awaitingDecision?: boolean;
  /** #1256：子智能体 roster 入口（会话区旁）。调用方（App）按"当前会话有无委派"
   *  决定渲染与否——无委派传 null/undefined，本组件不自行取数。 */
  rosterSlot?: ReactNode;
  /** #1372 改动 8：会话名 = 纯文本 + 下拉箭头（与状态 pill 区分）。会话列表与
   *  切换回调都是可选的——没传时退化为纯文本，既有测试夹具零迁移。 */
  sessions?: SessionSummary[];
  titlesById?: Record<string, string>;
  onSelectSession?: (sessionId: string) => void;
}

/** #1410：密度四档的菜单体。桌面档挂在独立的密度下拉里，≤560px 时同一个
 *  组件也挂进「更多工具」——顶栏只留一个菜单入口。两侧共用同一份受控状态
 *  （`density` / `onDensityChange`），不存在第二套密度真相。 */
function DensityRadioGroup({ density, onDensityChange }: Pick<Props, 'density' | 'onDensityChange'>) {
  return (
    <DropdownMenu.RadioGroup
      value={density}
      onValueChange={(value) => onDensityChange(value as TraceDensity)}
    >
      {DENSITIES.map((d) => (
        <DropdownMenu.RadioItem
          key={d}
          value={d}
          className="rail-menu-item appbar-density-item"
        >
          <span>{DENSITY_LABEL[d]}</span>
          <DropdownMenu.ItemIndicator>
            <Check size={14} aria-hidden="true" />
          </DropdownMenu.ItemIndicator>
        </DropdownMenu.RadioItem>
      ))}
    </DropdownMenu.RadioGroup>
  );
}

export function TopBar({ conversation, streaming, inspectorOpen, onToggleInspector, density, onDensityChange, theme, onToggleTheme, authRequired, onOpenMemories, onOpenApprovePolicy, onOpenRecoveryList, onOpenKanban, onOpenWatchtower, sessionId, onOpenContextUsage, onOpenContextWindow, awaitingDecision = false, rosterSlot, sessions, titlesById, onSelectSession }: Props) {
  // 身份 chip：订阅 token 变更（设置面板保存/清除即时反映），解码展示 claims。
  const [token, setTokenLive] = useState(getToken());
  useEffect(() => onTokenChange(() => setTokenLive(getToken())), []);
  const identity = useMemo(() => (token ? decodeJwtClaims(token) : null), [token]);

  // Auth 设置面板：token 是开发者设置项（localStorage ahi.apiToken）。
  // 输入框只在面板打开时同步真值——打开 = 读取当前存储，避免陈旧副本。
  const [authPanelOpen, setAuthPanelOpen] = useState(false);
  const [tokenDraft, setTokenDraft] = useState('');
  const authInputRef = useRef<HTMLInputElement>(null);
  /** 「更多工具」触发器：令牌面板从任何路径关闭时，焦点都回到这里。 */
  const moreTriggerRef = useRef<HTMLButtonElement>(null);
  /** 本次菜单关闭是否由「打开 API 身份令牌」引起：只在那一次阻止 Radix 归还焦点。
   *  不能按「面板此刻是否打开」判断——面板已开时用 Esc/其它菜单项关菜单也会被误吞。 */
  const authFocusPendingRef = useRef(false);

  // 流式计时（借鉴 ZCode 的"工作中 N 秒"）：进行中状态用真实秒数表达，而非空泛 spinner。
  const [elapsedSec, setElapsedSec] = useState(0);
  useEffect(() => {
    if (!streaming) {
      setElapsedSec(0);
      return;
    }
    const startedAt = Date.now();
    const timer = setInterval(
      () => setElapsedSec(Math.floor((Date.now() - startedAt) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [streaming]);

  const openAuthPanel = () => {
    setTokenDraft(getToken());
    const next = !authPanelOpen;
    authFocusPendingRef.current = next; // 只有「打开」才接管焦点
    setAuthPanelOpen(next);
  };

  // Radix 菜单关闭时会把焦点还给 trigger；若面板此刻打开，焦点要落到输入框，
  // 否则 Esc 落在 trigger 上、面板关不掉（键盘用户必须先手动点进来）。
  useEffect(() => {
    if (!authPanelOpen) return;
    const timer = window.setTimeout(() => authInputRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [authPanelOpen]);

  const saveToken = () => {
    setToken(tokenDraft);
    closeAuthPanel();
  };

  /** 关闭令牌面板并把焦点还给「更多工具」。面板内控件（输入框 / 保存 / 清除）
   *  随即卸载；不先移焦，键盘用户会掉回 body，下一次 Tab 从文档首重来。 */
  function closeAuthPanel() {
    setAuthPanelOpen(false);
    moreTriggerRef.current?.focus();
  }

  // #1030：`awaitingDecision` 是唯一不来自 `conversation` 的输入——坏死 run 的
  // run_status 停在 'running'，事件流里根本没有「卡在等人裁决」这个事实（唯一读面
  // 是后端只读端点）。所以它只能由 App 传进来，不能在这里派生。
  const pulse = deriveRunPulse(conversation, streaming, awaitingDecision);
  const PulseIcon = pulse.Icon;
  // 「待裁决」**不是** active：它没在跑（active 驱动 spinner 与耗时秒数，把它算进去
  // 就会给一个死掉的 run 计秒——正是本票要修的谎报）。
  const active = pulse.state === 'thinking' || pulse.state === 'tool';
  // #1313 口径 B：顶栏用量 = 会话累计（S + R），在渲染期派生（不是 effect 里的状态）。
  const sessionUsage = conversation ? displayedSessionUsage(conversation) : null;
  // #1372 改动 8：会话名（真实标题优先，没有标题时用短 id——不编造名字）。
  const sessionLabel = conversation
    ? (titlesById?.[conversation.session_id] || conversation.session_id.slice(0, 8))
    : '';
  // 归档会话默认不在侧栏显示；切换器同口径，不能给出一个侧栏里找不到的选项。
  const switchableSessions = useMemo(() => sessions?.filter((s) => !s.archived), [sessions]);
  const canSwitchSession = conversation !== null && onSelectSession !== undefined && (switchableSessions?.length ?? 0) > 1;
  // `exp` 来自未验签的 token claims：任何数字都能进来，`toISOString()` 对
  // 非有限/越界值会抛 RangeError，整条顶栏会跟着崩。无效就不显过期时间。
  const expDate = identity?.exp != null && Number.isFinite(identity.exp)
    ? new Date(identity.exp * 1000)
    : null;
  const expText = expDate !== null && !Number.isNaN(expDate.getTime())
    ? formatDateTime(expDate.toISOString())
    : null;

  // 停顿提示（FE-01/#148）：锚**空闲**（距上次新事件），不是流龄——健康的长任务里
  // 流龄一路涨，用它当阈值会把正常慢任务报成停顿。events.length 是投影真值的进度
  // 信号；只在「流已挂上且模型正在思考」时计时：说明文字断言的是「等待模型」，所以在
  // 工具执行（pulse 'tool'）与 run 已收口时都不能出现——那样顶栏会一边写
  // 「执行工具」一边写「仍在等待模型」，自相矛盾。
  // #427：审批等待是例外——等待的主语是用户，提示照出但文案换向（等审批）。
  // #443：判据与投影同源（projection.awaitingApproval，App.tsx 同款）——run 终结时
  // markPendingApprovalsStale 只置 stale 不移出数组，length > 0 ≠ 有事在等；
  // 失效审批不算，否则新 run 运行中会把等待的主语错说成"等你审批"。
  const awaitingApproval = hasLiveApproval(conversation?.pending_approvals ?? []);
  //
  // 进度信号走 ref 而不是 effect 依赖：依赖它会让**每个 delta** 都 teardown/重建一次
  // interval（快速流里每秒成百次）。这里 interval 只在 waiting 翻转时重建，每个事件只
  // 写一次 ref；同时把已显示的 idleSec 归零，免得新 chunk 到了还挂着上一轮的秒数。
  const progressKey = conversation?.events.length ?? 0;
  const progressAtRef = useRef(0);
  const [idleSec, setIdleSec] = useState(0);
  useEffect(() => {
    progressAtRef.current = Date.now();
    setIdleSec(0);
  }, [progressKey]);

  const waiting = shouldShowWaitHint(pulse.state, streaming, awaitingApproval);
  useEffect(() => {
    if (!waiting) {
      setIdleSec(0);
      return;
    }
    const timer = setInterval(
      () => setIdleSec(Math.floor((Date.now() - progressAtRef.current) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [waiting]);

  return (
    <header className="appbar">
      <div className="appbar-left">
        <OwletMark className="appbar-logo" />
        <span className="appbar-title">Owlet</span>
      </div>

      <div className="appbar-center">
        {conversation && (
          canSwitchSession ? (
            <DropdownMenu.Root>
              <Tip content={`当前会话：${sessionLabel} · 点击切换`}>
                <DropdownMenu.Trigger asChild>
                  <button
                    type="button"
                    className="appbar-session"
                    aria-label={`切换会话：${sessionLabel}`}
                  >
                    {/* 完整标题由 tooltip 承载，不再叠一层原生 title。 */}
                    <span className="appbar-session-name">
                      {sessionLabel}
                    </span>
                    <ChevronDown size={12} className="appbar-session-chevron" aria-hidden="true" />
                  </button>
                </DropdownMenu.Trigger>
              </Tip>
              <DropdownMenu.Portal>
                <DropdownMenu.Content
                  className="rail-menu appbar-session-menu"
                  align="start"
                  sideOffset={6}
                >
                  {switchableSessions?.map((s) => {
                    const label = titlesById?.[s.session_id] || s.session_id.slice(0, 8);
                    return (
                      <DropdownMenu.Item
                        key={s.session_id}
                        className="rail-menu-item appbar-session-menu-item"
                        aria-current={s.session_id === conversation.session_id ? 'true' : undefined}
                        onSelect={() => onSelectSession?.(s.session_id)}
                      >
                        <span className="appbar-session-item-title">{label}</span>
                        <span className="appbar-session-item-id mono num">
                          {s.session_id.slice(0, 8)}
                        </span>
                      </DropdownMenu.Item>
                    );
                  })}
                </DropdownMenu.Content>
              </DropdownMenu.Portal>
            </DropdownMenu.Root>
          ) : (
            <span className="appbar-session appbar-session-static">
              {sessionLabel}
            </span>
          )
        )}
        <span className={`run-pulse ${pulse.className}`}>
          <PulseIcon size={14} aria-hidden="true" />
          {pulse.label}
          {active && elapsedSec > 0 && <span className="num"> · {elapsedSec}s</span>}
          {/* Claude Code "(13s · 28 tokens)" 语言，**口径 B·会话累计（#1313）**：
              S（已终态 run 权威值之和）+ R（当前 run 流式累计），单次连续视图内单调非减；
              `run/completed` 以权威值**替换**本 run 的流式贡献，因此到达前后不出现
              数字变小的覆盖式跳变。无数据不显示（零伪造）。 */}
          {sessionUsage && sessionUsage.total_tokens !== null && (
            <span className="num"> · {sessionUsage.total_tokens.toLocaleString()} 字</span>
          )}
          {/* #537：run 预算徽标「已用 / 上限」——只出设了 ceiling 的维；数据源是
              投影的 run 预算镜像（run/started 回显 + run/resumed 更新），文本经
              budgetBadgeFacts 与暂停面板同源（同一张 RUN_DIMENSIONS 表、同一套
              文本规则），禁两套数字。≥80% 转静止预警色（常数非配置）。 */}
          {(conversation?.run_budget_ceilings
            ? budgetBadgeFacts(conversation.run_budget_ceilings, conversation.run_budget_consumed)
            : []
          ).map((fact) => (
            <span
              key={fact.spec.dimension}
              className={`num budget-badge${fact.warning ? ' budget-badge--warning' : ''}`}
            >
              {` · ${fact.consumedText} / ${fact.ceilingText} ${BUDGET_BADGE_UNITS[fact.spec.consumedKey]}`}
            </span>
          ))}
        </span>
        {waiting && <WaitingHint idleSec={idleSec} awaitingApproval={awaitingApproval} />}
        {rosterSlot}
      </div>

      <div className="appbar-right">
        {/* #1396：四段 segmented 压成单个下拉——触发器显示当前档，展开才出现四档。
            `RadioGroup` 保留「一组里只有一个选中」的语义（menuitemradio + aria-checked）。 */}
        <DropdownMenu.Root>
          <Tip content={`显示密度：${DENSITY_LABEL[density]} · 点击切换`}>
            <DropdownMenu.Trigger asChild>
              <button
                type="button"
                className="icon-btn appbar-icon-btn appbar-density-trigger"
                aria-label={`显示密度：${DENSITY_LABEL[density]}`}
              >
                <span>{DENSITY_LABEL[density]}</span>
                <ChevronDown size={12} aria-hidden="true" />
              </button>
            </DropdownMenu.Trigger>
          </Tip>
          <DropdownMenu.Portal>
            <DropdownMenu.Content
              className="rail-menu appbar-density-menu"
              align="end"
              sideOffset={6}
              aria-label="显示密度"
            >
              <DensityRadioGroup density={density} onDensityChange={onDensityChange} />
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
        {/* 身份 chip（da394a9 认证 UX）：token 已配置且可解码时展示 token 声称的身份。
            仅解码不验签——真伪由 401 拦截兜底；无 token / 畸形 token 不渲染。 */}
        {identity && (
          <Tip
            content={`tenant: ${identity.tenant_id ?? '—'} · user: ${identity.user_id ?? '—'}${
              expText ? ` · 过期于 ${expText}` : ''
            }`}
          >
            <span className="auth-chip" tabIndex={0}>
              {identity.user_id ?? '已配置'}
            </span>
          </Tip>
        )}
        {/* #1372 改动 6 / #1396：低频动作全部进 "…" 菜单。默认可见动作只剩密度下拉
            与「更多」两个；每一项仍是同一条既有回调，功能零丢失。 */}
        <DropdownMenu.Root>
          <Tip content="更多工具——记忆 / 看板 / 恢复 / 主题 / 身份令牌 / 上下文 / 审批 / Inspector">
            <DropdownMenu.Trigger asChild>
              <button
                ref={moreTriggerRef}
                className={`icon-btn appbar-icon-btn appbar-more-trigger${authRequired ? ' attention' : ''}`}
                aria-label="更多工具"
              >
                <Ellipsis size={16} />
              </button>
            </DropdownMenu.Trigger>
          </Tip>
          <DropdownMenu.Portal>
            <DropdownMenu.Content
              className="rail-menu appbar-more-menu"
              align="end"
              sideOffset={6}
              /* 选中「API 身份令牌」后菜单关闭：默认会把焦点还给触发器，
                 面板里的输入框就接不到 Esc。阻止这次归还，由面板 effect 接管。 */
              onCloseAutoFocus={(event) => {
                if (!authFocusPendingRef.current) return;
                authFocusPendingRef.current = false;
                event.preventDefault();
              }}
            >
              {/* #1410：≤560px 顶栏隐藏独立的密度触发器，四档收进这个菜单
                  （CSS 控制显隐；桌面档这里 display:none，不产生第二个入口）。 */}
              <div className="appbar-more-density">
                <DropdownMenu.Label className="appbar-more-density-label">
                  显示密度
                </DropdownMenu.Label>
                <DensityRadioGroup density={density} onDensityChange={onDensityChange} />
                <DropdownMenu.Separator className="rail-menu-sep" />
              </div>
              <DropdownMenu.Item className="rail-menu-item" onSelect={onOpenMemories}>
                <Brain size={14} aria-hidden="true" /> 记忆管理
              </DropdownMenu.Item>
              {onOpenKanban && (
                <DropdownMenu.Item className="rail-menu-item" onSelect={onOpenKanban}>
                  <Columns3 size={14} aria-hidden="true" /> Agent 看板
                </DropdownMenu.Item>
              )}
              {onOpenRecoveryList && (
                <DropdownMenu.Item className="rail-menu-item" onSelect={onOpenRecoveryList}>
                  <History size={14} aria-hidden="true" /> 恢复列表
                </DropdownMenu.Item>
              )}
              <DropdownMenu.Item className="rail-menu-item" onSelect={onToggleTheme}>
                {theme === 'dark' ? (
                  <Sun size={14} aria-hidden="true" />
                ) : (
                  <Moon size={14} aria-hidden="true" />
                )}
                切换主题
              </DropdownMenu.Item>
              <DropdownMenu.Separator className="rail-menu-sep" />
              <DropdownMenu.Item className="rail-menu-item" onSelect={openAuthPanel}>
                <KeyRound size={14} aria-hidden="true" /> API 身份令牌
                {authRequired && <span className="appbar-menu-warn">需要配置</span>}
              </DropdownMenu.Item>
              {sessionId && (
                <DropdownMenu.Item
                  className="rail-menu-item"
                  onSelect={() => onOpenContextUsage(sessionId)}
                >
                  <Gauge size={14} aria-hidden="true" /> 上下文容量
                </DropdownMenu.Item>
              )}
              {sessionId && onOpenContextWindow && (
                <DropdownMenu.Item
                  className="rail-menu-item"
                  onSelect={() => onOpenContextWindow(sessionId)}
                >
                  <SlidersHorizontal size={14} aria-hidden="true" /> 上下文窗口
                </DropdownMenu.Item>
              )}
              <DropdownMenu.Item className="rail-menu-item" onSelect={onOpenApprovePolicy}>
                <ShieldCheck size={14} aria-hidden="true" /> 持久审批规则
              </DropdownMenu.Item>
              {onOpenWatchtower && (
                <DropdownMenu.Item className="rail-menu-item" onSelect={onOpenWatchtower}>
                  <Timer size={14} aria-hidden="true" /> 自动值班
                </DropdownMenu.Item>
              )}
              <DropdownMenu.Separator className="rail-menu-sep" />
              <DropdownMenu.Item className="rail-menu-item" onSelect={onToggleInspector}>
                <PanelRight size={14} aria-hidden="true" />
                {inspectorOpen ? '收起 Inspector' : '展开 Inspector'}
              </DropdownMenu.Item>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        </DropdownMenu.Root>
      </div>

      {authPanelOpen && (
        <div className="auth-panel" role="dialog" aria-label="API 身份令牌设置" onKeyDown={(e) => e.key === 'Escape' && closeAuthPanel()}>
          <div className="auth-panel-title">API 身份令牌（Bearer）</div>
          <input
            ref={authInputRef}
            className="auth-panel-input"
            type="password"
            value={tokenDraft}
            placeholder="粘贴 HS256 token（eyJ…）"
            onChange={(e) => setTokenDraft(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && saveToken()}
            autoFocus
          />
          <div className="auth-panel-hint">
            仅配置了 <code>JWT_SECRET</code> 的部署需要；本地开发留空即可。
            Token 保存在浏览器 localStorage（<code>ahi.apiToken</code>），claims 需含
            tenant_id / user_id / exp（未过期）。
          </div>
          <div className="auth-panel-actions">
            <button className="auth-panel-save" onClick={saveToken}>保存</button>
            <button
              className="auth-panel-clear"
              onClick={() => {
                setToken('');
                closeAuthPanel();
              }}
            >
              清除
            </button>
          </div>
        </div>
      )}
    </header>
  );
}

/** 生成态里「等太久了」的诚实说明（FE-01/#148；#427 审批等待换主语）。
 *
 *  纯展示：只吃一个秒数入参（上游读的是投影真值 `events.length`），不发
 *  SessionEvent、不落库——刷新即消失，因此不构成第二套会话真相（不变量 #22）。
 *  判定用 `shouldShowWaitHint`、文案用 `waitingHintText`（两者的真相与依据都在
 *  `runState.ts`）；阈值以下的正常生成一个节点都不多渲染。 */
export function WaitingHint({ idleSec, awaitingApproval = false }: { idleSec: number; awaitingApproval?: boolean }) {
  const text = waitingHintText(idleSec, awaitingApproval);
  if (text === null) return null;
  return <span className="wait-hint">{text}</span>;
}

const DENSITY_LABEL: Record<TraceDensity, string> = {
  compact: '紧凑',
  balanced: '均衡',
  detailed: '详细',
  raw: 'Raw',
};
