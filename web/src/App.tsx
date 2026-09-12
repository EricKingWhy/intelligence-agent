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
import { useProjects } from './hooks/useProjects';
import { TopBar } from './components/TopBar';
import { SessionList } from './components/SessionList';
import { Conversation } from './components/Conversation';
import { Composer } from './components/Composer';
import { CommandPalette } from './components/CommandPalette';
import { MemoryPanel } from './components/MemoryPanel';
import { StepDetail, type InspectorFocus } from './components/StepDetail';
import { applyDensity, initDensity, type TraceDensity } from './lib/density';
import { useDisclosure, useReasoningDisclosure } from './lib/disclosure';
import { streamKeyFromEvent } from './lib/eventKind';
import { isPaletteShortcut, type CommandItem } from './lib/commands';
import { withTimeout } from './lib/timeout';
import { applyTheme, initTheme, type Theme } from './lib/theme';
import { isRecoverableRun, recoverDoneMessage } from './lib/runState';
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
import { toAmendFields, toCreateControls, type ComposerControls } from './lib/amend';
import type { ToolCall, PresetTask, AgentEvent, Project } from './types';
import './styles/app.css';

/** Workspace 模式 —— Chat 常驻；Split/Preview 为后续 Phase 预留的空架子。 */
type WorkspaceMode = 'chat' | 'split' | 'preview';
const WORKSPACE_MODES: readonly { id: WorkspaceMode; label: string; icon: typeof MessageSquare }[] = [
  { id: 'chat', label: 'Chat', icon: MessageSquare },
  { id: 'split', label: 'Split', icon: Columns2 },
  { id: 'preview', label: 'Preview', icon: Eye },
];

/** 分叉请求的兜底超时。`forkInFlightRef` 只在 `finally` 里复位——请求若既不
 *  resolve 也不 reject（socket 挂死），按钮会被永久静默禁用，正是本 ticket 要
 *  消灭的那类「点了没反应」。超时实现见 `lib/timeout`（记忆删除也用同一份，
 *  两个用途的语义提醒都写在那里）；api 层没有统一超时（其余请求同病）。 */
const FORK_TIMEOUT_MS = 30_000;

export default function App() {
  // ── Workspace 模式（Phase 1d，方案 B）──
  // Chat = 常驻阅读面，永不切换走（用户冻结决策）。
  // Split / Preview = 后续 Phase 升级的副面板，当前仅空架子（占位条），
  // 表明 Workspace 有自己的结构扩展点，但内容不由 tab 与 Inspector 抢走。
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>('chat');
  // BUG-001 fix：fork 失败的本地错误状态（useSession 的 error 是流级通道）。
  const [forkError, setForkError] = useState<{ sessionId: string; message: string } | null>(null);

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
    sendMessage,
    cancelStream,
    recover,
    refreshSessions,
    changeModel,
    fork,
  } = useSession();

  // ── 项目（WS-5 / #155）──
  // 侧栏"项目 → 会话"层级的数据源：**成员与顺序只有项目账本一个真相**
  // （GET /api/projects 的 session_ids，注册表序 + 手工序）；每行的
  // SessionSummary.workspace 只用来解释"自称属于某项目、但该项目不在列表里"的孤儿行，
  // 不参与判定归属（详见 lib/projects.ts 文件头注释）。前端只做投影（不变量 #22）。
  //
  // 为什么跟着 sessions 变：新会话（命名 workspace）、fork child、recover 都可能在
  // 后端**顺带**改变项目归属（#152 的 attach 接线），而它们唯一的信号就是会话列表被
  // 重新拉取。所以"列表刷新 ⇒ 项目重拉"是保持两个视图一致的最小机制；sessions 只在
  // refreshSessions 里换引用，不会随流式 delta 变化，不构成每帧请求。
  const {
    projects,
    loadError: projectsError,
    refresh: refreshProjects,
    actions: projectActions,
  } = useProjects();
  useEffect(() => {
    void refreshProjects();
  }, [sessions, refreshProjects]);

  // 「重试」同时重拉两个列表：项目列表失败时会话列表很可能也失败过（同一次网络
  // 抖动），只修一个会留下一个"半新鲜"的侧栏。必须 useCallback——SessionList 是
  // memo 组件，内联箭头会让它在每次流式 delta 上整片重渲染（同 handleSelect 一列）。
  const handleRetryProjects = useCallback(() => {
    void refreshProjects();
    void refreshSessions();
  }, [refreshProjects, refreshSessions]);

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
  // 四个控制目录（均已运行时消费，非 staged）：permission-modes /
  // agent-profiles / reasoning-efforts / context-providers。空 → 隐藏控件。
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

  /** 当前选中会话的 ref 镜像——异步回调（分叉落地）需要「落地时的当下值」，
   *  而闭包里的 selectedId 只是发起时的快照。镜像在 effect 里同步（render 期写
   *  ref 会被 react-hooks lint 判为 "Cannot update ref value during render"），
   *  但 effect 要等提交后 flush；切会话的两条入口额外**同步**写一次，把「点了
   *  另一个会话」到「镜像跟上」之间的窗口压到零。 */
  const selectedIdRef = useRef(selectedId);
  useEffect(() => {
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  const handleNew = useCallback(() => {
    // selectSession 内部处理流取消（切走即放弃当前流，幂等）
    selectedIdRef.current = null;
    selectSession(null);
    focusRun();
  }, [selectSession, focusRun]);

  const handleSelect = useCallback((id: string) => {
    selectedIdRef.current = id;
    selectSession(id);
    focusRun();
  }, [selectSession, focusRun]);

  useEffect(() => {
    void fetchModels();
    void fetchControlCatalogs();
  }, [fetchModels, fetchControlCatalogs]);

  const handleModelChange = useCallback(
    (name: string | null) => {
      setSelectedModel(name);
      // T7 #137：已有会话时，模型选择触发 POST /model 切换会话当前模型。
      // 新会话（无 selectedId）只更新本地状态——startSession 时携带 model。
      if (selectedId && name) {
        const entry = models.find((m) => m.name === name);
        if (entry?.provider) {
          void changeModel(selectedId, entry.provider, name)
            .then((result) => {
              // 用响应里的规范 model_id 更新本地状态（不回显请求值）
              setSelectedModel(result.model_id);
            })
            .catch(() => {
              // 切换失败静默——用户可重试；不阻塞主流程
            });
        }
      }
    },
    [selectedId, models, changeModel],
  );

  // Composer 档位打包（提交路径与 handleSubmit 的依赖数组共用同一引用）。
  // useMemo 而非内联对象：handleSubmit 是 useCallback，内联对象会让它每次
  // 渲染都换引用，Composer 的 memo 随之失效（流式期间每 delta 重渲染输入框）。
  const composerControls = useMemo<ComposerControls>(
    () => ({
      model: selectedModel,
      permissionMode: selectedPermissionMode,
      agentProfile: selectedAgentProfile,
      reasoningEffort: selectedReasoningEffort,
      contextProviders: selectedContextProviders,
    }),
    [
      selectedModel,
      selectedPermissionMode,
      selectedAgentProfile,
      selectedReasoningEffort,
      selectedContextProviders,
    ],
  );

  const handleSubmit = useCallback(
    (task: string) => {
      focusRun();
      // Composer 档位 → 契约字段的映射统一走 lib/amend.ts（单一构造器）；
      // 空值丢弃由 api 层单一执行（见 amend.ts 顶部契约说明）。
      // 续聊：已有会话且不在流式中 → 发消息到现有会话（PRD §5.3 续聊入口）。
      // 新会话：无 selectedId → startSession 创建新会话。
      if (selectedId && !streaming) {
        void sendMessage(selectedId, task, {
          maxSteps: 10,
          amend: toAmendFields(composerControls),
        });
        return;
      }
      void submitTask({
        task,
        max_steps: 10,
        auto_approve: true,
        ...toCreateControls(composerControls),
      });
    },
    [submitTask, sendMessage, focusRun, selectedId, streaming, composerControls],
  );

  /** 「在此项目中新建任务」（WS-6 / #169 AC11）：以项目路径为 cwd 起一个会话，
   *  复用 submitTask 的同一条 SSE 接线——新会话因此会被选中并跟随流，而不是另造
   *  一条"提交后就撒手"的路径（不变量 #22：会话真相只有一条）。
   *
   *  `ownError: true`：失败原因**返回给确认面**在浮层里就地显示（AC12），不打到
   *  Workspace 区的全局横幅上；同时那条路径里的 422 不套用「未知模型」旧语义，
   *  所以「目录不存在：…」这类后端 detail 会原样出现在用户眼前。
   *
   *  `permissionMode === null`（默认档）→ 不进 payload → api 层不发键 → 后端
   *  默认 workspace-write + auto-approve（见 StartTaskInProjectDialog 文件头）。 */
  const handleStartTaskInProject = useCallback(
    (project: Project, task: string, permissionMode: string | null) =>
      submitTask(
        {
          task,
          cwd: project.path,
          max_steps: 10,
          auto_approve: true,
          ...(permissionMode ? { permission_mode: permissionMode } : {}),
        },
        { ownError: true },
      ),
    [submitTask],
  );

  /** T7 #137：从历史用户消息 seq 派生 child session，成功后跳转到 child。
   *
   *  分叉是异步的，而它的两个结局都会动用户视野（跳 child / 弹错误条），
   *  所以结局落地前要确认用户还停在发起时那个会话上（同 shouldApplyStreamFrame
   *  的 stale-write 纪律）——切走了就别把 A 会话的结论贴到 B 会话界面上。 */
  /** 在途分叉的 **origin session id**（不是布尔）。按钮没有 pending 态，连点会在
   *  后端造出两个 child 会话，所以同一会话在途时要挡；但挡的范围必须限定在
   *  「同一个 origin」——用全局布尔的话，A 会话的分叉还在飞时切到 B 会话点分叉
   *  会被静默丢弃，又回到本 ticket 要消灭的「点了没反应」。 */
  const forkInFlightRef = useRef<string | null>(null);

  const handleFork = useCallback(
    async (fromSeq: number) => {
      if (!selectedId || forkInFlightRef.current === selectedId) return;
      forkInFlightRef.current = selectedId;
      const origin = selectedId;
      setForkError(null);
      try {
        const result = await withTimeout(fork(origin, fromSeq), FORK_TIMEOUT_MS, '分叉请求');
        if (selectedIdRef.current !== origin) return;
        // 跳转到 child session（同步镜像，见 selectedIdRef 注释）
        selectedIdRef.current = result.session_id;
        selectSession(result.session_id);
        void refreshSessions();
      } catch (e) {
        if (selectedIdRef.current !== origin) return;
        // BUG-001 fix：分叉失败不再静默——展示后端 detail。
        setForkError({ sessionId: origin, message: `分叉失败：${(e as Error).message}` });
      } finally {
        // 只清自己那一格：期间可能已有另一个会话的分叉在途
        if (forkInFlightRef.current === origin) forkInFlightRef.current = null;
      }
    },
    [selectedId, fork, selectSession, refreshSessions],
  );

  // 已不含所选 name（死选中值），校正回默认链，避免无效 422 循环。
  // 识别走 useSession 具名判定（submitTask 不抛出，error 是其唯一对外通道）。
  // 同步刷新控制目录并清除死选中值（permission_mode / agent_profile /
  // reasoning_effort 同样可能因目录变更而 422）。
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
      setSelectedContextProviders((prev: string[]) => prev.filter((id) => providers.some((p) => p.id === id)));
    })();
  }, [error]);

  // ── Recover 入口可见性（da394a9 §二.2 后端建议语义）──
  // isRecoverableRun：最后 run 缺终态（completed/failed/interrupted 都没有）或
  // 存在未配对 tool_call（含恢复后仍悬空的那部分——所以 recover 成功后入口
  // 会自己消失）。干净失败的 run 是终态——不再显示恢复入口（旧条件会误标）。
  // useMemo：isRecoverableRun 要扫全量事件（run 状态一遍 + dangling 配对一遍，
  // 共两趟），而 App 在流式期间每个 delta 都渲染一次；不记忆化就是每帧一份
  // O(events)。（依赖里 conversation 每帧都是新对象 → 记忆化在这里其实救不了
  // 流式帧；真正让它在流式期间不跑的是 `!streaming` 短路。留着是为了非流式下
  // 的重复渲染不重扫。）
  const canRecover = useMemo(
    () =>
      selectedId !== null &&
      !streaming &&
      !loadingHistory &&
      conversation !== null &&
      isRecoverableRun(conversation.events),
    [selectedId, streaming, loadingHistory, conversation],
  );

  // ── Command Palette（PRD §15，ADR-0014）：Ctrl/Cmd+K 开关 + 命令集组装 ──
  const [paletteOpen, setPaletteOpen] = useState(false);
  // 记忆管理浮层（MEM-5 / #160）：开合状态归 App（顶栏按钮与命令面板共用同一入口）。
  const [memoriesOpen, setMemoriesOpen] = useState(false);
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
    // label 用中文（与工具栏/空态/提示文案一致——此前只有 label 是英文、hint 已是
    // 中文，属本地化做了一半），英文说法放进 keywords 继续可搜（BUG-007）。
    const items: CommandItem[] = [
      {
        id: 'toggle-inspector',
        label: '切换 Run Inspector',
        keywords: 'toggle run inspector 右栏',
        hint: '右栏',
        group: 'actions',
        run: () => {
          userToggledRef.current = true;
          setInspectorOpen((v) => !v);
        },
      },
      {
        id: 'jump-latest',
        label: '跳到最新事件',
        keywords: 'jump to latest event 定位',
        hint: '定位',
        group: 'actions',
        run: () => {
          if (!conversation || conversation.events.length === 0) return;
          const last = conversation.events[conversation.events.length - 1];
          focusEvent(last);
          jumpToStream(last);
        },
      },
      // run_id 缺则该命令不出现：留着会是个「点了没反应」的假按钮。
      // （它此前抄的是 `conversation.session_id`——标签说 Run ID、动作给 Session ID，
      //   两者都是 UUID，粘出去只能用错地方才发现。）
      ...(conversation?.run_id
        ? [{
            id: 'copy-run-id',
            label: '复制 Run ID',
            keywords: 'copy run id',
            hint: conversation.run_id.slice(0, 12),
            group: 'actions' as const,
            run: () => copyText(conversation.run_id!),
          }]
        : []),
      {
        id: 'copy-trace-id',
        label: '复制 Trace ID',
        keywords: 'copy trace id langfuse',
        hint: conversation?.trace_id ? 'Langfuse' : undefined,
        group: 'actions',
        run: () => {
          // PRD §15 "Copy Trace ID（若存在）"：不存在不出现该命令。
        },
      },
      {
        id: 'open-trace',
        label: '打开 Trace',
        keywords: 'open trace langfuse',
        hint: conversation?.trace_url ? 'Langfuse ↗' : undefined,
        group: 'actions',
        run: () => {
          // 契约 2d7f87a：trace_url 缺则该命令不出现（splice 移除）。
        },
      },
      {
        id: 'toggle-theme',
        label: '切换主题',
        keywords: 'toggle theme dark light 暗色 亮色',
        // hint 也走中文：它是**显示文本**，langfuse 那种专有名词才保留英文（BUG-007 同一类）。
        hint: theme === 'dark' ? '→ 亮色' : '→ 暗色',
        group: 'actions',
        run: toggleTheme,
      },
      {
        id: 'focus-composer',
        label: '聚焦输入框',
        keywords: 'focus composer',
        hint: '输入框',
        group: 'actions',
        run: () => document.getElementById('composer-input')?.focus(),
      },
      {
        id: 'manage-memories',
        label: '管理记忆',
        keywords: 'memory memories forget delete 记忆 遗忘 删除 忘记',
        hint: '记忆库',
        group: 'actions',
        run: () => setMemoriesOpen(true),
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
    // 密度命令的 label 与工具栏同名（紧凑/均衡/详细/Raw），英文档位名进 keywords。
    const DENSITY_CN = { compact: '紧凑', balanced: '均衡', detailed: '详细', raw: 'Raw' } as const;
    for (const d of ['compact', 'balanced', 'detailed', 'raw'] as const) {
      items.push({
        id: `density-${d}`,
        label: `切换到${DENSITY_CN[d]}`,
        // 末尾不再重复一次 ${d}：实测「重复的尾 token」会让大量无意义的 3 字符
        // query（如 aac/aca）只靠这层重复命中，纯增噪，而正当匹配一次都不受益。
        keywords: `switch to ${d} density 密度`,
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
        onOpenMemories={() => setMemoriesOpen(true)}
      />

      <main className={`app-regions ${inspectorOpen ? '' : 'inspector-closed'}`}>
        <SessionList
          sessions={sessions}
          projects={projects}
          selectedId={selectedId}
          liveSessionId={streaming ? selectedId : null}
          titlesById={titlesById}
          onSelect={handleSelect}
          onNew={handleNew}
          projectActions={projectActions}
          onSessionsChanged={refreshSessions}
          projectsError={projectsError}
          onRetryProjects={handleRetryProjects}
          onStartTask={handleStartTaskInProject}
          permissionModes={permissionModes}
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
          {/* 分叉失败提示带上它属于哪个会话：只属于发起它的那个会话，切走自然
              不再渲染（不用 effect 清空——那会多一次渲染，也会留下「清空」与
              「切会话」两份状态需要同步）。 */}
          {forkError && forkError.sessionId === selectedId && (
            <div className="app-error" role="alert">{forkError.message}</div>
          )}
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
                <span className="recover-hint">该会话缺少 run 终态或有未配对工具调用——可尝试恢复</span>
              )}
            </div>
          )}
          {/* 恢复成功提示**必须在 canRecover 门外**：修好后 dangling 归零、
              入口随 canRecover 一起消失，提示若挂在门内会立刻被卸载——
              等于用户又什么都看不到（这正是本缺陷的原始症状）。
              文案由 recoverDoneMessage 统一给出：回填 N 条 / 补齐 run 终态 / 两者
              都有 / 后端没修完（附**具体**原因，可重试）/ 真无可修。两个「没修完」
              的原因是分开传的——canRecover 是 OR，压成布尔就会把原因说错（终态已
              补、只是还有悬空 tool_call 时报「仍缺 run 终态」）。 */}
          {recoverState.status === 'done' && !streaming && (
            <div className="recover-done" role="status" aria-live="polite">
              {recoverDoneMessage({
                repaired: recoverState.repaired,
                terminalRepaired: recoverState.terminalRepaired,
                stillUnterminated: recoverState.stillUnterminated,
                stillDangling: recoverState.stillDangling,
              })}
            </div>
          )}
          {conversation?.run_interrupted && !streaming && (
            <div className="interrupt-banner" role="status" aria-live="polite">
              {/* step_id 可能缺失/null，且这是**真值不是缺失**：进程在该 run 的第一个
                  带步号事件之前就死了（run/started 本身不带 step，检测器只能
                  一路沿用后续事件的 step_id）。实测扫 71 个真实会话：4 条
                  run/interrupted 里有 2 条信封整个不带 step_id（run/started 后
                  紧接 interrupted，如 c63ce4d3-3b26-40bb-8e8c-e3af8dd33035）——
                  渲染成「第 ? 步」等于把「还没开始就断了」说成一个未知数字。*/}
              {conversation.run_interrupted.step_id !== null
                ? `上次运行在第 ${conversation.run_interrupted.step_id} 步中断`
                : '上次运行在首个步骤开始前中断'}
              （原因：{conversation.run_interrupted.reason}）
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
            onFork={handleFork}
          />
          <Composer
            streaming={streaming}
            /* UI-01：待决审批 > 0 → composer 锁定（同一 projection 状态，无第二真相源）。 */
            approvalPending={(conversation?.pending_approvals.length ?? 0) > 0}
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
            selectedContextProviders={selectedContextProviders}
            onContextProvidersChange={setSelectedContextProviders}
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
      <MemoryPanel open={memoriesOpen} onOpenChange={setMemoriesOpen} />
    </div>
  );
}
