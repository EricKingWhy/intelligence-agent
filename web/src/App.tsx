/** App — single-frame application shell (Phase 2, Brief §5/§6).
 *
 * ONE solid frame: App Bar on top, three regions below separated by 1px
 * dividers — Session Rail | Agent Workspace | Run Inspector. No floating
 * cards, no per-panel shadows (Brief §6.1 "One Shell, Not Three Cards").
 *
 * Responsive (frozen decision: collapse ≠ unmount):
 *   - ≥1200px: all three regions
 *   - <1200px: Inspector collapses (toggle in App Bar), stays mounted
 *   - <820px:  Rail collapses to 56px icon rail (overlay drawer deferred)
 *
 * Panel geometry is transient — NOT persisted (invariant #22: no second truth).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Columns2, Eye, KeyRound, MessageSquare, RotateCcw, X } from 'lucide-react';
import { isUnknownModelError, useSession } from './hooks/useSession';
import { TopBar } from './components/TopBar';
import { SessionList } from './components/SessionList';
import { Conversation } from './components/Conversation';
import { Composer } from './components/Composer';
import { CommandPalette } from './components/CommandPalette';
import { StepDetail, type InspectorFocus } from './components/StepDetail';
import { applyDensity, initDensity, type TraceDensity } from './lib/density';
import { useDisclosure, useReasoningDisclosure } from './lib/disclosure';
import { streamKeyFromEvent } from './lib/eventKind';
import { isPaletteShortcut, type CommandItem } from './lib/commands';
import { applyTheme, initTheme, type Theme } from './lib/theme';
import { isRecoverableRun } from './lib/runState';
import { onTokenChange, onUnauthorized } from './lib/auth';
import {
  getAgentProfiles,
  getContextProviders,
  getModels,
  getPermissionModes,
  getReasoningEfforts,
  type CatalogEntry,
  type ModelCatalogEntry,
} from './lib/api';
import { summarizeEvent } from './lib/projection';
import type { ToolCall, PresetTask, AgentEvent } from './types';
import './styles/app.css';

/** Workspace 模式 —— Chat 常驻；Split/Preview 为后续 Phase 预留的空架子。 */
type WorkspaceMode = 'chat' | 'split' | 'preview';
const WORKSPACE_MODES: readonly { id: WorkspaceMode; label: string; icon: typeof MessageSquare }[] = [
  { id: 'chat', label: 'Chat', icon: MessageSquare },
  { id: 'split', label: 'Split', icon: Columns2 },
  { id: 'preview', label: 'Preview', icon: Eye },
];

export default function App() {
  // ── Workspace 模式（Phase 1d，方案 B）──
  // Chat = 常驻阅读面，永不切换走（用户冻结决策）。
  // Split / Preview = 后续 Phase 升级的副面板，当前仅空架子（占位条），
  // 表明 Workspace 有自己的结构扩展点，但内容不由 tab 与 Inspector 抢走。
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>('chat');

  const {
    sessions,
    selectedId,
    conversation,
    loadingHistory,
    streaming,
    reconnecting,
    error,
    titlesById,
    recoverState,
    selectSession,
    submitTask,
    cancelStream,
    recover,
    refreshSessions,
  } = useSession();

  // ── Auth 接缝（df4f7d8 §1.2 fail-closed）──
  // 401 由 api.ts 统一拦截并广播；这里只负责展示引导横幅。配置 token 后
  // 自动清横幅并重试会话列表（onTokenChange），无需整页刷新。
  // ── T10 #103 模型目录（GET /api/models，契约 C6）──
  // 加载失败/端点缺席 → models=[] → Composer 选择器降级隐藏（不伪造列表）。
  // selectedModel=null = 默认链（提交不带 model 字段，默认链行为不变）。
  const [models, setModels] = useState<ModelCatalogEntry[]>([]);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const fetchModels = useCallback(async () => {
    try {
      setModels(await getModels());
    } catch {
      setModels([]); // 降级隐藏入口——错误不打扰（非关键能力）
    }
  }, []);

  // ── Phase 2b Composer control row（Ticket F1）──
  // 四个 staged 契约清单：permission-modes / agent-profiles /
  // reasoning-efforts / context-providers。空 → 隐藏控件。
  const [permissionModes, setPermissionModes] = useState<CatalogEntry[]>([]);
  const [selectedPermissionMode, setSelectedPermissionMode] = useState<string | null>(null);
  const [agentProfiles, setAgentProfiles] = useState<CatalogEntry[]>([]);
  const [selectedAgentProfile, setSelectedAgentProfile] = useState<string | null>(null);
  const [reasoningEfforts, setReasoningEfforts] = useState<CatalogEntry[]>([]);
  const [selectedReasoningEffort, setSelectedReasoningEffort] = useState<string | null>(null);
  const [contextProviders, setContextProviders] = useState<CatalogEntry[]>([]);
  const [selectedContextProviders, setSelectedContextProviders] = useState<string[]>([]);
  const fetchControlCatalogs = useCallback(async () => {
    try {
      const [modes, profiles, efforts, providers] = await Promise.all([
        getPermissionModes(),
        getAgentProfiles(),
        getReasoningEfforts(),
        getContextProviders(),
      ]);
      setPermissionModes(modes);
      setAgentProfiles(profiles);
      setReasoningEfforts(efforts);
      setContextProviders(providers);
    } catch {
      // 降级隐藏——非关键能力
      setPermissionModes([]);
      setAgentProfiles([]);
      setReasoningEfforts([]);
      setContextProviders([]);
    }
  }, []);
  const [authRequired, setAuthRequired] = useState(false);
  useEffect(() => onUnauthorized(() => setAuthRequired(true)), []);
  useEffect(
    () =>
      onTokenChange(() => {
        setAuthRequired(false);
        void refreshSessions();
        void fetchModels(); // T10：模型目录同样吃鉴权缝——配置 token 后补拉
        void fetchControlCatalogs(); // 控制目录也走 apiFetch 认证缝——补拉
      }),
    [refreshSessions, fetchModels, fetchControlCatalogs],
  );

  // 密度四档（冻结决策）：状态在 App（TopBar 切换、Conversation 消费），persist 由 lib/density 负责。
  const [density, setDensity] = useState<TraceDensity>(initDensity);
  const changeDensity = (next: TraceDensity) => {
    setDensity(next);
    applyDensity(next);
  };

  // Esc 中断（Claude Code "esc to interrupt" 语言）：流式中 Esc = 停止当前 run，
  // 与 Composer 停止按钮同走 cancelStream。dialog 打开时（palette/auth 面板）
  // Esc 优先归它们——target 在 dialog 内则不抢。target 可能是 window/document
  //（合成事件/焦点缺失），closest 仅对 Element 存在——先做类型守卫。
  useEffect(() => {
    if (!streaming) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      const t = e.target;
      if (t instanceof Element && t.closest('[role="dialog"]')) return;
      cancelStream();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [streaming, cancelStream]);

  // 主题状态归 App（TopBar 按钮与 Command Palette Toggle Theme 共享）。
  const [theme, setTheme] = useState<Theme>(initTheme);
  const toggleTheme = useCallback(() => {
    setTheme((cur) => {
      const next: Theme = cur === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      return next;
    });
  }, []);

  // L0-L2 展开状态（ADR-0014 D2）：全局 density 给默认级，手动 override 优先；
  // 随选中会话切换清空（sessionKey = selectedId）。
  const disclosure = useDisclosure(selectedId);
  // T2（#95）reasoning 自动开合（S6/S7）：streaming 默认开、完成自动收、
  // 手动 override 永久优先；同随会话切换清空。
  const reasoningDisclosure = useReasoningDisclosure(selectedId, density);

  // Inspector 焦点（Brief "上下文 Inspector"）：Run 级 ↔ 事件级，一键返回，不用弹窗。
  // 全部 useCallback：下游 SessionList/Composer/Conversation/StepDetail 的 memo
  // 依赖引用稳定的回调，普通函数每次渲染新引用会让 memo 全部失效。
  const [focus, setFocus] = useState<InspectorFocus>({ kind: 'run' });
  const focusRun = useCallback(() => setFocus({ kind: 'run' }), []);
  const focusTool = useCallback((tool: ToolCall) => setFocus({ kind: 'tool', tool }), []);
  const focusEvent = useCallback((event: AgentEvent) => setFocus({ kind: 'event', event }), []);
  // Phase 13 委派钻取（v2 PRD §10.5）：委派节点 Inspect → 右栏原位展开 child
  // 会话；深层钻取是显式意图——Inspector 关着时一并打开（不同于 hover Inspect）。
  const focusChild = useCallback(
    (child: { childSessionId: string; target: string }) => {
      setFocus({ kind: 'child', ...child });
      setInspectorOpen(true);
    },
    [],
  );

  // Main↔Inspector 联动（PRD §9，ADR-0014 D5）：
  //   正向：中间 hover Inspect → focusTool/focusEvent（Inspector 打开 + 详情切换）。
  //   反向：Inspector Timeline 点行 → 中间主区滚动定位 + pulse（jumpRequest nonce
  //   保证重复跳同一目标也触发 Conversation effect）。
  const [jumpRequest, setJumpRequest] = useState<{ key: string; nonce: number } | null>(null);
  const jumpToStream = useCallback((event: AgentEvent) => {
    const key = streamKeyFromEvent(event.data, event.step_id);
    if (key) setJumpRequest({ key, nonce: Date.now() });
  }, []);
  // 空状态示例任务 → 注入 Composer（对象引用变化触发注入，可重复点击）
  const [presetTask, setPresetTask] = useState<PresetTask | null>(null);
  const onPresetTask = useCallback((text: string) => setPresetTask({ text, id: Date.now() }), []);
  // Inspector 折叠是视图状态：收起不卸载（DSH 语义，冻结决策）。
  // 窄屏（<1200px）默认收起；用户手动切换后以手动值优先（仅本会话内，不持久化）。
  const [inspectorOpen, setInspectorOpen] = useState(
    () => typeof window === 'undefined' || window.innerWidth >= 1200,
  );
  const userToggledRef = useRef(false);
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 1200px)');
    const onChange = (e: MediaQueryListEvent) => {
      if (!userToggledRef.current) setInspectorOpen(!e.matches);
    };
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);

  const toggleInspector = () => {
    userToggledRef.current = true;
    setInspectorOpen((v) => !v);
  };

  const handleNew = useCallback(() => {
    // selectSession 内部处理流取消（切走即放弃当前流，幂等）
    selectSession(null);
    focusRun();
  }, [selectSession, focusRun]);

  const handleSelect = useCallback((id: string) => {
    selectSession(id);
    focusRun();
  }, [selectSession, focusRun]);

  useEffect(() => {
    void fetchModels();
    void fetchControlCatalogs();
  }, [fetchModels, fetchControlCatalogs]);

  const handleModelChange = useCallback((name: string | null) => {
    setSelectedModel(name);
  }, []);

  const handleSubmit = useCallback(
    (task: string) => {
      focusRun();
      void submitTask({
        task,
        max_steps: 10,
        auto_approve: true,
        ...(selectedModel ? { model: selectedModel } : {}),
        ...(selectedPermissionMode ? { permission_mode: selectedPermissionMode } : {}),
        ...(selectedAgentProfile ? { agent_profile: selectedAgentProfile } : {}),
        ...(selectedReasoningEffort ? { reasoning_effort: selectedReasoningEffort } : {}),
        ...(selectedContextProviders.length > 0
          ? { context_providers: selectedContextProviders }
          : {}),
      });
    },
    [
      submitTask,
      focusRun,
      selectedModel,
      selectedPermissionMode,
      selectedAgentProfile,
      selectedReasoningEffort,
      selectedContextProviders,
    ],
  );

  // 422 = 未知模型（契约 C6）：目录可能已变——自动刷新一次；刷新后若目录
  // 已不含所选 name（死选中值），校正回默认链，避免无效 422 循环。
  // 识别走 useSession 具名判定（submitTask 不抛出，error 是其唯一对外通道）。
  // 同步刷新控制目录并清除死选中值（permission_mode / agent_profile /
  // reasoning_effort 的 staged 契约同样可能因目录变更而 422）。
  useEffect(() => {
    if (!error || !isUnknownModelError(error)) return;
    void (async () => {
      const [modelList, modes, profiles, efforts, providers] = await Promise.all([
        getModels().catch(() => [] as ModelCatalogEntry[]),
        getPermissionModes().catch(() => [] as CatalogEntry[]),
        getAgentProfiles().catch(() => [] as CatalogEntry[]),
        getReasoningEfforts().catch(() => [] as CatalogEntry[]),
        getContextProviders().catch(() => [] as CatalogEntry[]),
      ]);
      setModels(modelList);
      setSelectedModel((prev) => (prev && modelList.some((m) => m.name === prev) ? prev : null));
      setPermissionModes(modes);
      setSelectedPermissionMode((prev) => (prev && modes.some((m) => m.id === prev) ? prev : null));
      setAgentProfiles(profiles);
      setSelectedAgentProfile((prev) => (prev && profiles.some((m) => m.id === prev) ? prev : null));
      setReasoningEfforts(efforts);
      setSelectedReasoningEffort((prev) => (prev && efforts.some((m) => m.id === prev) ? prev : null));
      setContextProviders(providers);
    })();
  }, [error]);

  // ── Recover 入口可见性（da394a9 §二.2 后端建议语义）──
  // isRecoverableRun：最后 run 缺终态（completed/failed 都没有）或存在未配对
  // tool_call。干净失败的 run 是终态——不再显示恢复入口（旧条件会误标）。
  const canRecover =
    selectedId !== null &&
    !streaming &&
    !loadingHistory &&
    conversation !== null &&
    isRecoverableRun(conversation.events);

  // ── Command Palette（PRD §15，ADR-0014）：Ctrl/Cmd+K 开关 + 命令集组装 ──
  const [paletteOpen, setPaletteOpen] = useState(false);
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (isPaletteShortcut(e)) {
        e.preventDefault();
        setPaletteOpen((v) => !v);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  const copyText = useCallback((text: string) => {
    void navigator.clipboard.writeText(text).catch(() => {
      /* 剪贴板不可用（非安全上下文等）：静默——复制是尽力而为动作 */
    });
  }, []);

  const paletteItems = useMemo<CommandItem[]>(() => {
    const items: CommandItem[] = [
      {
        id: 'toggle-inspector',
        label: 'Toggle Run Inspector',
        hint: '右栏',
        group: 'actions',
        run: () => {
          userToggledRef.current = true;
          setInspectorOpen((v) => !v);
        },
      },
      {
        id: 'jump-latest',
        label: 'Jump to Latest Event',
        hint: '定位',
        group: 'actions',
        run: () => {
          if (!conversation || conversation.events.length === 0) return;
          const last = conversation.events[conversation.events.length - 1];
          focusEvent(last);
          jumpToStream(last);
        },
      },
      {
        id: 'copy-run-id',
        label: 'Copy Run ID',
        hint: conversation ? conversation.session_id.slice(0, 12) : undefined,
        group: 'actions',
        run: () => conversation && copyText(conversation.session_id),
      },
      {
        id: 'copy-trace-id',
        label: 'Copy Trace ID',
        hint: conversation?.trace_id ? 'Langfuse' : undefined,
        group: 'actions',
        run: () => {
          // PRD §15 "Copy Trace ID（若存在）"：不存在不出现该命令。
        },
      },
      {
        id: 'open-trace',
        label: 'Open Trace',
        hint: conversation?.trace_url ? 'Langfuse ↗' : undefined,
        group: 'actions',
        run: () => {
          // 契约 2d7f87a：trace_url 缺则该命令不出现（splice 移除）。
        },
      },
      {
        id: 'toggle-theme',
        label: 'Toggle Theme',
        hint: theme === 'dark' ? '→ Light' : '→ Dark',
        group: 'actions',
        run: toggleTheme,
      },
      {
        id: 'focus-composer',
        label: 'Focus Composer',
        hint: '输入框',
        group: 'actions',
        run: () => document.getElementById('composer-input')?.focus(),
      },
    ];
    // trace_id 缺则 Copy Trace ID 不出现；trace_url 缺则 Open Trace 不出现
    // （Langfuse 未启用时两者都 null，两个命令都移除；启用时 Copy 恒在、Open 看 trace_url）。
    if (conversation?.trace_id) {
      items[items.findIndex((c) => c.id === 'copy-trace-id')].run = () => copyText(conversation.trace_id!);
    } else {
      items.splice(items.findIndex((c) => c.id === 'copy-trace-id'), 1);
    }
    if (conversation?.trace_url) {
      items[items.findIndex((c) => c.id === 'open-trace')].run = () =>
        window.open(conversation.trace_url!, '_blank', 'noopener');
    } else {
      items.splice(items.findIndex((c) => c.id === 'open-trace'), 1);
    }
    for (const d of ['compact', 'balanced', 'detailed', 'raw'] as const) {
      items.push({
        id: `density-${d}`,
        label: `Switch to ${d[0].toUpperCase()}${d.slice(1)}`,
        hint: d === density ? '当前' : undefined,
        group: 'density',
        run: () => changeDensity(d),
      });
    }
    // Search Runtime Events（PRD §15）：最近事件直出为可选项（选中即定位）。
    if (conversation) {
      const events = conversation.events.slice(-100).reverse(); // 新→旧
      events.forEach((e, i) => {
        const summary = summarizeEvent(e);
        items.push({
          id: `event-${conversation.events.length - 1 - i}`,
          label: `${e.type}${summary ? ` · ${summary}` : ''}`,
          hint: e.seq !== null ? `#${e.seq}` : undefined,
          group: 'events',
          run: () => {
            focusEvent(e);
            jumpToStream(e);
            if (!inspectorOpen) {
              userToggledRef.current = true;
              setInspectorOpen(true);
            }
          },
        });
      });
    }
    return items;
  }, [conversation, density, theme, toggleTheme, copyText, jumpToStream, focusEvent, inspectorOpen]);

  return (
    <div className="app-frame">
      <TopBar
        conversation={conversation}
        streaming={streaming}
        inspectorOpen={inspectorOpen}
        onToggleInspector={toggleInspector}
        density={density}
        onDensityChange={changeDensity}
        theme={theme}
        onToggleTheme={toggleTheme}
        authRequired={authRequired}
      />

      <main className={`app-regions ${inspectorOpen ? '' : 'inspector-closed'}`}>
        <SessionList
          sessions={sessions}
          selectedId={selectedId}
          liveSessionId={streaming ? selectedId : null}
          titlesById={titlesById}
          onSelect={handleSelect}
          onNew={handleNew}
        />

        <section className="app-workspace">
          {authRequired && (
            <div className="auth-banner" role="alert">
              <KeyRound size={14} />
              <span>
                后端要求身份令牌（401）：点击顶栏 <strong>钥匙图标</strong> 配置 Bearer Token
                即自动重试——本地开发环境（未配置 JWT_SECRET）不应出现此提示。
              </span>
              <button
                className="auth-banner-close"
                onClick={() => setAuthRequired(false)}
                aria-label="关闭提示"
              >
                <X size={14} />
              </button>
            </div>
          )}
          {error && <div className="app-error">{error}</div>}
          {reconnecting && (
            // T4（#97）断线状态条：瞬时重连不清屏不轰炸——conversation 照常
            // 累积，条只在重连期间在场（aria-live 播报一次状态变化）。
            <div className="reconnect-banner" role="status" aria-live="polite">
              <span className="reconnect-dot" aria-hidden="true" />
              连接中断，正在重连…
            </div>
          )}
          {canRecover && (
            <div className="workspace-toolbar">
              <button
                className="recover-btn"
                onClick={() => selectedId && void recover(selectedId)}
                disabled={recoverState.status === 'pending'}
                title="修复中断会话：按 Operation Ledger 回填工具结果、标记 dangling 调用（幂等）"
              >
                <RotateCcw size={14} />
                {recoverState.status === 'pending' ? '恢复中…' : '恢复会话'}
              </button>
              {recoverState.status === 'error' && !recoverState.conflict && (
                <span className="recover-error">{recoverState.message}</span>
              )}
              {recoverState.status === 'error' && recoverState.conflict && (
                <span className="recover-conflict" title={recoverState.message ?? ''}>
                  需人工裁决：{recoverState.message}
                </span>
              )}
              {recoverState.status === 'idle' && (
                <span className="recover-hint">最后事件非 run/completed——可尝试恢复</span>
              )}
            </div>
          )}
          {/* Workspace 模式条（Phase 1d 方案 B）：Chat 永远是主阅读面，
              Split/Preview 为后续 Phase 预留的空架子。条本身克制——
              只在选中非 chat 时渲染下方占位行；Chat 模式下完全不占垂直空间。 */}
          <div className="workspace-mode-bar" role="toolbar" aria-label="Workspace 模式">
            {WORKSPACE_MODES.map((m) => {
              const Icon = m.icon;
              const sel = workspaceMode === m.id;
              return (
                <button
                  key={m.id}
                  type="button"
                  aria-pressed={sel}
                  className={`workspace-mode ${sel ? 'sel' : ''}`}
                  onClick={() => setWorkspaceMode(m.id)}
                  title={m.label}
                >
                  <Icon size={13} className="workspace-mode-icon" aria-hidden="true" />
                  <span className="workspace-mode-label">{m.label}</span>
                </button>
              );
            })}
          </div>
          {workspaceMode !== 'chat' && (
            <div className="workspace-scaffold" role="note">
              <span className="workspace-scaffold-tag">未来升级点</span>
              <span className="workspace-scaffold-text">
                {workspaceMode === 'split'
                  ? 'Split：副面板显示同一会话的另一视图（代码 diff / 预览）。当前仍以 Chat 为主阅读面。'
                  : 'Preview：副面板渲染当前会话产出的 Artifact（文档 / 图表 / 页面）。当前仍以 Chat 为主阅读面。'}
              </span>
            </div>
          )}
          <Conversation
            conversation={conversation}
            loadingHistory={loadingHistory}
            density={density}
            disclosure={disclosure}
            reasoningDisclosure={reasoningDisclosure}
            jumpRequest={jumpRequest}
            onPresetTask={onPresetTask}
            onFocusTool={focusTool}
            onOpenSession={handleSelect}
            onInspectChild={focusChild}
          />
          <Composer
            streaming={streaming}
            onSubmit={handleSubmit}
            onCancel={cancelStream}
            presetTask={presetTask}
            models={models}
            selectedModel={selectedModel}
            onModelChange={handleModelChange}
            permissionModes={permissionModes}
            selectedPermissionMode={selectedPermissionMode}
            onPermissionModeChange={setSelectedPermissionMode}
            agentProfiles={agentProfiles}
            selectedAgentProfile={selectedAgentProfile}
            onAgentProfileChange={setSelectedAgentProfile}
            reasoningEfforts={reasoningEfforts}
            selectedReasoningEffort={selectedReasoningEffort}
            onReasoningEffortChange={setSelectedReasoningEffort}
            contextProviders={contextProviders}
          />
        </section>

        <StepDetail
          conversation={conversation}
          streaming={streaming}
          focus={focus}
          onFocusRun={focusRun}
          onFocusTool={focusTool}
          onFocusEvent={focusEvent}
          onJumpToStream={jumpToStream}
        />
      </main>

      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} items={paletteItems} />
    </div>
  );
}
