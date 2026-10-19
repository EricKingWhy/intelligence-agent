/** SessionList — Session Rail of the single-frame shell（Phase 2 Brief §10；WS-5 #155 改版）。
 *
 * 两层结构：
 *   项目（注册表顺序）→ 会话（**账本手工序**，用户拖出来的顺序，不按活动时间重排）
 *   未分组 → 不属于任何项目的会话（保持后端给的活动时间序）
 *
 *  三条不变量：
 *  1. **不丢行**：分组由 `lib/projects.ts` 的 `buildRailModel` 派生，未出现在任何账本
 *     里的会话（含 workspace 引用已失效项目的孤儿行）一律落进未分组区。
 *  2. **不改真相**：顺序与归属都来自后端账本（`GET /api/projects` 的 `session_ids`）；
 *     `GET /api/sessions` 的 `workspace` 只用来解释"自称属于某项目、但该项目不在
 *     当前列表里"的孤儿行（`staleProject`），不参与判定成员资格。本组件不维护影子
 *     成员名单，每次写操作后重新拉取（`ProjectActions` 内部 refresh + `onSessionsChanged`）。
 *  3. **失败要说话**：项目写操作失败时把后端 detail 原样贴在侧栏顶部（`rail-error`），
 *     而不是静默或只留一个禁用的按钮。
 *
 *  时间分组（Running/Today/Yesterday/Older）在 WS-5 后**只保留 Running 的活体语义**：
 *  行上的绿点 + 呼吸仍表示"该会话正在流式"，但不再用时间桶切分列表——侧栏现在有
 *  真实的层级要表达，两套切分叠在一起会让同一行出现在两个不同的分类逻辑下。
 */

import { memo, useMemo, useRef, useState } from 'react';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import {
  Archive,
  ArrowDown,
  ArrowUp,
  ChevronRight,
  Ellipsis,
  Folder,
  FolderOpen,
  FolderPlus,
  Plus,
  Puzzle,
  TriangleAlert,
} from 'lucide-react';
import type { Project, SessionDeleted, SessionSummary } from '../types';
import { formatRelativeTime } from '../lib/format';
import { useTickingNow } from '../hooks/useTickingNow';
import { buildRailModel, dropAnchor, moveAnchor } from '../lib/projects';
import { describeOwnership } from '../lib/ownership';
import { readShowArchived, writeShowArchived } from '../lib/railArchive';
import type { UngroupedRow } from '../lib/projects';
import { describeProjectError } from '../lib/api';
import type {
  ModelCatalogEntry,
  SandboxBackendEntry,
  StartSessionPayload,
} from '../lib/api';
import type { ProjectActions } from '../hooks/useProjects';
import {
  AttachToProjectDialog,
  CreateProjectDialog,
  DeleteProjectDialog,
  InlineRename,
} from './ProjectDialogs';
import { DeleteSessionDialog, type DeleteSessionTarget } from './DeleteSessionDialog';
import { SessionReplayDialog, type ReplaySessionTarget } from './SessionReplayDialog';
import { TaskCreationDialog } from './TaskCreationDialog';
import { SessionListSkeleton } from './SessionListSkeleton';

interface Props {
  sessions: SessionSummary[];
  /** #1371：会话列表首次加载中（骨架屏）。缺省 false = 既有调用方/测试的语义。 */
  sessionsLoading?: boolean;
  projects: Project[];
  selectedId: string | null;
  /** The session currently receiving a live stream (live dot). */
  liveSessionId: string | null;
  /** 行标题缓存（Session Model E 轮）：键为 session_id，值为首条 user/message 投影。
   *  无缓存的会话回退到短 ID——不伪造标题。 */
  titlesById: Record<string, string>;
  onSelect: (id: string) => void;
  onNew: () => void;
  /** #1028：当前被"选中"的项目行（= 通用新建要继承 cwd 的项目语境）。null = 无。 */
  activeProjectId: string | null;
  /** 点中项目行 → 记为项目语境（再点另一个则替换）。 */
  onSelectProject: (projectId: string | null) => void;
  /** #1028 AC3：选中会话的 cwd（后端 `session/started.cwd` 的投影）。
   *  只在**选中且未归属**的那一行用于就地标注归属；null = 未加载/未声明。 */
  selectedCwd: string | null;
  projectActions: ProjectActions;
  /** 会话语义发生变化后刷新列表（attach/detach/软删除都会改各行的 `workspace`）。 */
  onSessionsChanged: () => void;
  /** `GET /api/projects` 失败原因（有值时项目区**不隐藏**，只是多一条可重试的错误条：
   *  隐藏项目会把所有会话误显示成未分组，比显示一条错误糟）。 */
  projectsError: string | null;
  /** `GET /api/sessions` 失败原因（F9）。与 `projectsError` **同形同因**——两者都是
   *  "区域级加载失败"，都不该走主区那条**单槽** error 横幅（会被后续错误覆盖掉，
   *  真机实测过：见 `docs/LIVE_BROWSER_TEST_20260917.md` §8.5）。 */
  sessionsError: string | null;
  onRetryProjects: () => void;
  /** 「新建任务…」（#367 [W-23] 选项 A；#1177 §A.3 改名，原「在此项目中新建任务」）：
   *  统一创建入口，prompt 唯一必填 + 三档自主度 + 模型 + 运行位置，创建即启动。
   *  resolve `null` = 已创建；否则为**给用户看的原因**（留在确认面里）。 */
  onStartTask: (payload: StartSessionPayload) => Promise<string | null>;
  /** 模型目录（GET /api/models）——创建表单模型选择器的数据源。 */
  models: ModelCatalogEntry[];
  /** sandbox 后端探针（GET /api/sandbox-backends）——运行位置选择器的数据源。 */
  sandboxBackends: SandboxBackendEntry[];
  /** 硬删一个会话（#172 / ADR-0029，**不可恢复**）：失败原因抛给确认面显示，
   *  成功后的状态收敛（清当前视图 / 重拉列表）由 useSession.removeSession 负责
   *  ——本组件不碰会话状态，只把用户点的那一行交出去。 */
  onDeleteSession: (sessionId: string) => Promise<SessionDeleted>;
  /** 归档 / 取消归档一个会话（#171，**可逆**）：resolve `null` = 成功（列表已刷新）；
   *  否则是**给用户看的原因**（后端 detail 原文）——就地显示在本栏顶部，不弹全局横幅
   *  （AC9）。可逆动作因此不需要确认面（AC11）：再点一次「取消归档」就是撤销。 */
  onSetArchived: (sessionId: string, archived: boolean) => Promise<string | null>;
  /** #1200：打开「插件」管理窗口（侧栏一级入口，常驻）。 */
  onOpenPlugins: () => void;
}

// memo：流式期间本组件 props（sessions/projects/selectedId/titlesById/回调）全部引用
// 稳定，跳过每个 delta 的会话栏重渲染；相对时间刷新由内部 useTickingNow 独立驱动。
export const SessionList = memo(function SessionList({
  sessions,
  sessionsLoading = false,
  projects,
  selectedId,
  liveSessionId,
  titlesById,
  onSelect,
  onNew,
  activeProjectId,
  onSelectProject,
  selectedCwd,
  projectActions,
  onSessionsChanged,
  projectsError,
  sessionsError,
  onRetryProjects,
  onStartTask,
  models,
  sandboxBackends,
  onDeleteSession,
  onSetArchived,
  onOpenPlugins,
}: Props) {
  const now = useTickingNow();
  /** 「显示已归档」开关（#171 AC10）：**视图状态**，持久化到 localStorage 但**不重拉
   *  列表**——载荷本来就是完整一份（见 `lib/api.ts::listSessions` 的分层说明），
   *  开关只改投影，所以切换是瞬时的、不闪屏。 */
  const [showArchived, setShowArchived] = useState(readShowArchived);
  // 「项目有哪些会话」= 账本投影，不另存一份（不变量 #22）。
  const model = useMemo(
    () => buildRailModel(sessions, projects, { includeArchived: showArchived }),
    [sessions, projects, showArchived],
  );
  const visibleCount =
    model.groups.reduce((n, g) => n + g.sessions.length, 0) + model.ungrouped.length;
  /** 归档条数取**载荷的真值**，不拿 `sessions.length - visibleCount` 推——两者在
   *  "项目账本占用了同一 id"等边界下会不等（见 `buildRailModel` 的 claimed 占用表），
   *  而这句话是在向用户陈述事实。 */
  const archivedCount = sessions.filter((s) => s.archived).length;

  // 折叠状态是**视图**状态（不持久化，与 Inspector 折叠同一立场）。
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [createOpen, setCreateOpen] = useState(false);
  const [deleting, setDeleting] = useState<Project | null>(null);
  /** 待硬删的会话（null = 确认面关闭）——与项目软删分开一个状态：两者的后果与
   *  文案都不同（一个只摘注册记录，一个不可恢复），共用会让"点错了哪一类"没法表达。 */
  const [deletingSession, setDeletingSession] = useState<DeleteSessionTarget | null>(null);
  /** 待重放的会话（`#1322`，null = 浮层关闭）。与硬删分开一个状态：重放只读、零
   *  副作用、可无限重来，与不可恢复的删除是两种后果，共用会让入口语义含混。 */
  const [replayTarget, setReplayTarget] = useState<ReplaySessionTarget | null>(null);
  const [attachFor, setAttachFor] = useState<string | null>(null);
  /** 「新建任务…」的目标项目（null = 确认面关闭）。 */
  const [startTaskFor, setStartTaskFor] = useState<Project | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  /** #945 / #1177：这一次菜单关闭是否要**跳过** Radix 默认的"归还 trigger 焦点"。
   *
   *  两种菜单项会把焦点**主动**移到别处，晚到的归还（Radix `onCloseAutoFocus` 在
   *  `setTimeout` 里的 `trigger.focus()`）会把它偷回来：
   *   - #945「重命名项目」：焦点交给就地挂载的 InlineRename 输入框（`autoFocus`），
   *     晚到的归还会在输入框挂载后立刻 blur 它，`InlineRename.onBlur` 遂把编辑态
   *     取消（含刚显示的空白名错误提示）——e2e 里表现为 `getByLabel('项目名')` 在
   *     两个断言之间凭空消失。
   *   - #1177「新建会话」：焦点交给 composer（`onNew` → `handleNew` 里的
   *     `#composer-input` focus），晚到的归还会
   *     把光标从"马上要打字"的输入框拽回 kebab 按钮——直达入口的意义就没了。
   *  其余菜单项（开对话框 / 纯动作）保持默认的焦点归还。
   *
   *  不变量：同一时刻只有一个项目菜单能开着，且 Radix 每次关闭都会触发一次
   *  `onCloseAutoFocus` ⇒ 置位后必在紧接着的那次关闭里被消费复位，标志不跨关闭残留。 */
  const suppressMenuAutoFocus = useRef(false);
  const [opError, setOpError] = useState<string | null>(null);
  // 拖拽重排（HTML5 DnD）：记**来源项目**而不只是被拖的会话 id——跨项目拖动
  // 不是重排（那是 attach，有 cwd 校验），落点提示只能在来源项目内亮。
  const [drag, setDrag] = useState<{ projectId: string; sessionId: string } | null>(null);
  const [dropTarget, setDropTarget] = useState<{ projectId: string; before: string | null } | null>(
    null,
  );

  /** 选中会话所在的项目永远保持展开：折叠一个"里面有当前会话"的项目等于把用户
   *  正在看的行藏起来——界面不该出现这种自相矛盾的状态。 */
  const isExpanded = (project: Project) =>
    !collapsed.has(project.id) || project.session_ids.includes(selectedId ?? '');

  const toggle = (id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const ledgerOf = (projectId: string) =>
    projects.find((p) => p.id === projectId)?.session_ids ?? [];

  /** 项目写操作的统一出口：失败把后端原因贴到侧栏顶部，绝不静默。
   *  （对话框自己的提交路径不走这里——它们的错误留在对话框里更贴近操作。） */
  const runOp = async (label: string, run: () => Promise<unknown>) => {
    try {
      await run();
      setOpError(null);
      onSessionsChanged();
    } catch (e) {
      setOpError(describeProjectError(e, label));
    }
  };

  const handleMove = (projectId: string, sessionId: string, dir: 'up' | 'down') => {
    const anchor = moveAnchor(ledgerOf(projectId), sessionId, dir);
    if (!anchor) return; // 边界（按钮已禁用，这里是双保险）
    void runOp('调整顺序失败', () => projectActions.reorder(projectId, sessionId, anchor.before));
  };

  const resetDrag = () => {
    setDrag(null);
    setDropTarget(null);
  };

  /** 拖放落定：`before` = 放到这一行前面，null = 放到该项目队尾。 */
  const handleDrop = (projectId: string, before: string | null) => {
    const dragged = drag;
    resetDrag();
    if (!dragged) return;
    // 只支持**项目内**拖动重排：未分组的行拖到项目上等于 attach，而 attach 有
    // cwd 校验（可能 409）——那条路径走"加入项目…"，这里不猜。跨项目同理。
    if (dragged.projectId !== projectId) return;
    const ids = ledgerOf(projectId);
    if (!ids.includes(dragged.sessionId)) return;
    const anchor = dropAnchor(ids, dragged.sessionId, before);
    if (!anchor) return;
    void runOp('调整顺序失败', () =>
      projectActions.reorder(projectId, dragged.sessionId, anchor.before),
    );
  };

  /** 归档 / 取消归档一行：失败原因就地显示（`opError` 那条栏），成功后沿用
   *  `onSetArchived` 里的列表刷新——本组件不碰会话真相，只把用户的意图交出去
   *  （与 `onDeleteSession` 同一分工）。 */
  const handleSetArchived = async (sessionId: string, archived: boolean) => {
    setOpError(await onSetArchived(sessionId, archived));
  };

  const toggleArchived = () => {
    setShowArchived((prev) => {
      const next = !prev;
      writeShowArchived(next);
      return next;
    });
  };

  const showEmpty = !sessionsLoading && sessions.length === 0 && projects.length === 0;
  /** 会话列表**没拿到**（不是"确实没有"）——F9：`sessions === []` 同时是"真没有会话"
   *  与"这次请求失败了"的形状，两者**必须分开说**。
   *
   *  真机症状（`docs/LIVE_BROWSER_TEST_20260917.md` §8.5）：后端不可达时，项目区挂着
   *  「项目列表加载失败：… 重试」，同一屏的会话区却写着「暂无会话，提交任务即可开始。」
   *  ——把一次加载失败渲染成一个看起来完全正常的空状态；用户此刻最想知道的恰恰是
   *  "我的会话还在不在"。空态判据只有 `length === 0`，所以它无法自己分辨这件事；
   *  分辨所需的唯一信息在本栏之外（请求结果），故以 prop 传入。
   *
   *  注意它**不等于** `sessionsError !== null` 之外还要看别的：只要这次没拿到，
   *  无论之前有没有成功过都不说"暂无会话"（有旧数据时列表照旧渲染，空态本来就不成立）。 */
  const sessionsUnavailable = sessionsError !== null;
  /** 首屏骨架的挂载条件：只有「首次请求未 settle 且还没有任何行」才算 loading。
   *  后续刷新（已有列表）不闪骨架。 */
  const sessionsPending = sessionsLoading && sessions.length === 0;

  return (
    <aside className="session-rail">
      <div className="session-list-header">
        <span className="panel-label">会话</span>
        {/* #1200：插件是**一级常驻入口**（不随会话/项目增删出现或消失）。 */}
        <button
          className="icon-btn rail-plugins-btn"
          onClick={onOpenPlugins}
          aria-label="插件"
          title="插件：MCP Servers 与 Skills 的启用 / 禁用"
        >
          <Puzzle size={14} />
        </button>
        {/* UI-05：真空态下两个 icon 按钮切换为**带文字**的按钮——空态文案引导
            「右上角的『新建项目』」时，用户必须能在右上角看到一个叫这个名字的东西
            （识别而非回忆：全 Rail icon-only 时该文案指涉的文字实体不存在）。 */}
        {showEmpty ? (
          <div className="session-list-actions rail-empty-actions">
            <button className="rail-empty-btn-primary" onClick={() => setCreateOpen(true)}>
              新建项目
            </button>
            <button className="rail-empty-btn-ghost" onClick={onNew}>
              新会话
            </button>
          </div>
        ) : (
          <div className="session-list-actions">
            {/* #171：「显示已归档」——`aria-pressed` 是开关的语义（不是两个动作按钮），
                按下态给 accent 色（CSS）。icon-only 与本栏其余动作一致。 */}
            <button
              className={`icon-btn rail-archived-toggle${showArchived ? ' on' : ''}`}
              aria-pressed={showArchived}
              onClick={toggleArchived}
              aria-label="显示已归档会话"
              title="显示已归档会话（归档只改列表可见性，不删任何东西）"
            >
              <Archive size={14} />
            </button>
            <button
              className="icon-btn"
              onClick={() => setCreateOpen(true)}
              aria-label="新建项目"
              title="新建项目：注册一个已存在的目录"
            >
              <FolderPlus size={14} />
            </button>
            <button className="icon-btn new-session-btn" onClick={onNew} aria-label="新建会话">
              <Plus size={14} />
            </button>
          </div>
        )}
      </div>

      {/* `opError` 优先（动作级、更具体），它出现时**不再并列**列表加载错误：归档走
          「写成功 → 紧接着重拉列表失败」那个窗口时，动作级那句（「归档已生效，但会话
          列表刷新失败…」，见 `useSession.setArchived`）说的就是同一件事的更好版本，
          并列两条只是噪音。这条优先级与改动前一致（原先那条 else-if 链里 opError 也
          在最前），不是新语义。
          两条**列表**错误条彼此独立（不是 else-if 链）：它们说的是两件不同的事实，
          一次挂两条不是重复而是"确实坏了两处"。真机 F9 现场就是这样——后端不可达时
          项目与会话**两个**列表同时 502，用链式结构只会显示第一条，第二条那个
          「暂无会话」的假空态就仍然没人挡（`docs/LIVE_BROWSER_TEST_20260917.md` §8.5）。 */}
      {opError ? (
        <div className="rail-error" role="alert">
          <span className="rail-error-text" title={opError}>
            {opError}
          </span>
          <button
            className="rail-error-close"
            onClick={() => setOpError(null)}
            aria-label="关闭错误提示"
          >
            ×
          </button>
        </div>
      ) : null}
      {!opError && projectsError ? (
        <div className="rail-error" role="alert">
          <span className="rail-error-text" title={projectsError}>
            项目列表加载失败：{projectsError}
          </span>
          <button className="rail-error-retry" onClick={onRetryProjects}>
            重试
          </button>
        </div>
      ) : null}
      {!opError && sessionsError ? (
        <div className="rail-error" role="alert">
          <span className="rail-error-text" title={sessionsError}>
            {sessionsError}
          </span>
          {/* 复用项目的重试回调：`handleRetryProjects` 本来就同时重拉项目与会话两个列表
              （`App.tsx`），再包一层只是为了改回调而改回调。 */}
          <button className="rail-error-retry" onClick={onRetryProjects}>
            重试
          </button>
        </div>
      ) : null}

      <div className="session-items">
        {sessionsPending ? (
          <SessionListSkeleton />
        ) : (
          <>
            {showEmpty && !sessionsUnavailable && (
              <div className="empty-hint">暂无会话，提交任务即可开始。</div>
            )}
            {/* 不是真空态，但开关把**每一行**都收起来了——必须说清楚，否则侧栏看起来像坏了
                （且给出去处：顶部那个开关）。
                两个守卫都不能省：
                - `sessions.length > 0`：项目已注册但还没有任何会话时 `visibleCount` 同样是 0，
                  不加这条会渲染出「0 条都已归档」——把"没有会话"说成"都归档了"（不变量 #21）。
                - `!showArchived`：开关已经开着还提示"点开关查看"，指向一个已经按下的按钮。
                数字取真正归档的条数而不是 `sessions.length`：后者把未归档但也看不见的行
                （理论上不存在的状态）也算进去，是在替数据说话。 */}
            {!showEmpty && sessions.length > 0 && !showArchived && visibleCount === 0 && (
              <div className="empty-hint">
                没有可见的会话：{archivedCount} 条都已归档。点侧栏顶部的
                <Archive size={12} aria-hidden="true" /> 图标查看。
              </div>
            )}
          </>
        )}

        {/* 项目区**不**被会话骨架替换：会话与项目是两条独立请求，
            若 `listSessions` 悬挂，整块被骨架盖住会让项目区永久不可达。骨架只代表
            「会话行未到」，项目结构照常可见；loading 窗口内不渲染依赖 sessions 的
            计数/缺失提示，避免把"还没拿到"说成"没有会话"。 */}
        <section className="rail-section" aria-label="项目">
          {projects.length > 0 ? (
            <>
              <div className="rail-section-label">项目</div>
              {model.groups.map(({ project, sessions: rows, missing }) => {
                const expanded = isExpanded(project);
                const listId = `rail-project-${project.id}`;
                const renaming = renamingId === project.id;
                // #1269：本行是否是「通用新建会话」当前继承 cwd 的项目语境（#1028 的 toggle）。
                const contextArmed = activeProjectId === project.id;
                // 落点提示只在本项目内亮：跨项目拖动不是重排（见 handleDrop）。
                const dropEnabled = drag?.projectId === project.id;
                // 会话行未到的窗口里 `rows` 必然是空数组——此时计数取项目账本的
                // 归属数（唯一可得的事实），否则会显示「0 个会话」并渲染空项目提示。
                const rowCount = sessionsPending ? project.session_ids.length : rows.length;
                return (
                  <section className="rail-project" key={project.id}>
                    <div className="rail-project-head">
                      {renaming ? (
                        // 重命名时不再渲染 toggle 按钮：input 不能嵌在 button 里
                        // （HTML 不允许交互内容嵌套），所以整块换成同布局的 div。
                        <div className="rail-project-toggle rail-project-toggle-static">
                          <ChevronRight size={12} className="rail-project-chevron open" aria-hidden="true" />
                          <FolderOpen size={13} className="rail-project-icon" aria-hidden="true" />
                          <InlineRename
                            initial={project.title}
                            onCommit={(title) => {
                              setRenamingId(null);
                              if (title && title !== project.title) {
                                void runOp('重命名失败', () =>
                                  projectActions.rename(project.id, title),
                                );
                              }
                            }}
                            onCancel={() => setRenamingId(null)}
                          />
                        </div>
                      ) : (
                        <button
                          className={`rail-project-toggle${
                            contextArmed ? ' project-active' : ''
                          }`}
                          onClick={() => {
                            // #1028：点中项目行 = 选中项目语境（通用新建据此继承 cwd）
                            // + 既有展开/收起。两个语义可以共存——"进入这个项目的视图"
                            // 正是用户表达"我要在这个项目里干活"的动作。
                            // **再次点击已 active 的行 = 取消语境**（onSelectProject(null)）：
                            // 可逆语义——用户为浏览而点开项目后能显式退出，不会被动地为
                            // 下一次通用新建武装 cwd（审查 P3）。
                            // #1269：这**是一个 toggle**——展开/收起与语境武装共用同一次点击，
                            // 故在行上给出显式状态提示（下方 `rail-project-armed-hint` +
                            // title/aria-label），让用户一眼看出当前是「新建会话目标」、可再点取消。
                            onSelectProject(contextArmed ? null : project.id);
                            toggle(project.id);
                          }}
                          aria-expanded={expanded}
                          aria-controls={expanded ? listId : undefined}
                          // 窄屏（≤820px）标题与计数被 CSS 收起，而 display:none 会把
                          // 它们从可访问性树里摘掉——不补 aria-label 这个按钮在窄屏
                          // 就是无名控件（#179）。
                          // #1269：武装态语义也进可访问名称。**刻意不写「新建会话」四字**——
                          // 那是侧栏「新建会话」按钮的 a11y 名称，而 `getByRole('button',
                          // { name: '新建会话' })` 默认**按子串匹配**，本行名称一旦含它就会与
                          // 侧栏按钮双命中（strict mode 违例，实测把 e2e `project-context` AC1
                          // 打红）。改说「新会话」既传达同一状态，又不与那个入口撞名。
                          // 武装态文案共三处（本 aria-label / 下方 title / 可见 chip），改动须同步。
                          aria-label={`${project.title}：${
                            expanded ? '收起项目' : `展开项目（${rowCount} 个会话）`
                          }${contextArmed ? '；已选中为新会话的目标目录，再点一次取消' : ''}`}
                          title={`${expanded ? '收起项目' : `展开项目（${rowCount} 个会话）`}${
                            contextArmed ? ' · 已选中为「新建会话」目标目录（再点一次取消）' : ''
                          }`}
                        >
                          <ChevronRight
                            size={12}
                            className={`rail-project-chevron${expanded ? ' open' : ''}`}
                            aria-hidden="true"
                          />
                          {expanded ? (
                            <FolderOpen size={13} className="rail-project-icon" aria-hidden="true" />
                          ) : (
                            <Folder size={13} className="rail-project-icon" aria-hidden="true" />
                          )}
                          <span className="rail-project-title" title={project.path}>
                            {project.title}
                          </span>
                          {/* #1269：语境武装的显式状态提示——标出"这是通用新建会话的目标"。
                              `aria-hidden`：同义已进上方 `aria-label`，避免读屏重复播报。 */}
                          {contextArmed && (
                            <span className="rail-project-armed-hint" aria-hidden="true">
                              新建会话目标
                            </span>
                          )}
                          {project.status === 'missing-dir' && (
                            <TriangleAlert
                              size={12}
                              className="rail-project-warn"
                              role="img"
                              aria-label="目录当前不存在"
                            />
                          )}
                          <span className="rail-project-count num">{rowCount}</span>
                        </button>
                      )}
                      <DropdownMenu.Root>
                        <DropdownMenu.Trigger asChild>
                          <button
                            className="icon-btn rail-menu-btn"
                            aria-label={`项目「${project.title}」操作`}
                          >
                            <Ellipsis size={14} />
                          </button>
                        </DropdownMenu.Trigger>
                        <DropdownMenu.Portal>
                          <DropdownMenu.Content
                            className="rail-menu"
                            align="end"
                            sideOffset={4}
                            // #945 / #1177：选中「重命名项目」/「新建会话」时焦点已被主动
                            // 移到别处，别再让关闭时默认的"归还 trigger 焦点"偷回来（见
                            // `suppressMenuAutoFocus` 注释）。其余情况保持 Radix 默认。
                            onCloseAutoFocus={(event) => {
                              if (suppressMenuAutoFocus.current) {
                                suppressMenuAutoFocus.current = false;
                                event.preventDefault();
                              }
                            }}
                          >
                            {/* #1177 §A：项目菜单两项分开——「新建会话」直达空会话（不经
                                表单，复用侧栏「+」的惰性创建链路）；「新建任务…」弹化简
                                后的对话框（创建即启动）。省略号 = 点击会弹窗（Apple HIG /
                                GNOME 惯例），与"直接进"一眼区分。
                                都放最前：它们是这个项目行最常用、也最不容易误伤的操作——
                                重命名 / 删除在下面。 */}
                            <DropdownMenu.Item
                              className="rail-menu-item"
                              onSelect={() => {
                                // 「新建会话」= 复用通用新建链路，唯一差异是先用 #1028 的
                                // 预设项目语境机制把项目 cwd 记成语境（onSelectProject）：
                                // 首条消息提交时才真正创建会话、并带 `cwd: project.path`
                                // 直接诞生在项目目录里并自动入组（不弹任何表单）。
                                // 不得照搬后丢失 cwd 锚定——这条就是锚定的接缝。
                                // 焦点归 composer（`onNew` → `handleNew` focus
                                // `#composer-input`）：跳过 Radix 关闭时的 trigger
                                // 归还，否则光标会被拽回 kebab（见 ref 注释）。
                                suppressMenuAutoFocus.current = true;
                                onSelectProject(project.id);
                                onNew();
                              }}
                            >
                              新建会话
                            </DropdownMenu.Item>
                            <DropdownMenu.Item
                              className="rail-menu-item"
                              onSelect={() => setStartTaskFor(project)}
                            >
                              新建任务…
                            </DropdownMenu.Item>
                            <DropdownMenu.Separator className="rail-menu-sep" />
                            <DropdownMenu.Item
                              className="rail-menu-item"
                              onSelect={() => {
                                suppressMenuAutoFocus.current = true;
                                setRenamingId(project.id);
                              }}
                            >
                              重命名项目
                            </DropdownMenu.Item>
                            <DropdownMenu.Item
                              className="rail-menu-item rail-menu-item-danger"
                              onSelect={() => setDeleting(project)}
                            >
                              删除项目…
                            </DropdownMenu.Item>
                          </DropdownMenu.Content>
                        </DropdownMenu.Portal>
                      </DropdownMenu.Root>
                    </div>

                    {expanded && (
                      <div className="rail-project-rows" id={listId}>
                        {rows.length === 0 && !sessionsPending && (
                          // 空项目不再引导用户"去未分组找 cwd 匹配的会话"——WS-6 之后
                          // 在项目里直接开任务就是最短路径，旧文案的语义已被本入口取代
                          // （AC10：占位区替换为该入口按钮）。
                          // #1177 §A.3：入口随菜单一项改名（「在此项目中新建任务」→
                          // 「新建任务…」），两处表面用同一文案。
                          <div className="rail-project-empty">
                            这个项目还没有会话。
                            <button
                              className="rail-empty-action"
                              onClick={() => setStartTaskFor(project)}
                            >
                              新建任务…
                            </button>
                          </div>
                        )}
                        {rows.map((s) => (
                          <SessionRow
                            key={s.session_id}
                            session={s}
                            title={titlesById[s.session_id] ?? ''}
                            selected={selectedId === s.session_id}
                            live={liveSessionId === s.session_id}
                            now={now}
                            projectId={project.id}
                            ledger={project.session_ids}
                            dragging={drag?.sessionId === s.session_id}
                            dropEnabled={dropEnabled}
                            dropBefore={
                              dropTarget?.projectId === project.id &&
                              dropTarget.before === s.session_id
                            }
                            onSelect={onSelect}
                            onMove={handleMove}
                            onDetach={(sessionId) =>
                              void runOp('移出项目失败', () =>
                                projectActions.detach(project.id, sessionId),
                              )
                            }
                            onAttachPick={setAttachFor}
                            onOpenCreate={() => setCreateOpen(true)}
                            onDragStartRow={(sessionId) =>
                              setDrag({ projectId: project.id, sessionId })
                            }
                            onDragEndRow={resetDrag}
                            onDragOverRow={(before) =>
                              setDropTarget({ projectId: project.id, before })
                            }
                            onDropRow={(before) => handleDrop(project.id, before)}
                            onDelete={setDeletingSession}
                            onSetArchived={(sessionId, archived) =>
                              void handleSetArchived(sessionId, archived)
                            }
                            onReplay={setReplayTarget}
                          />
                        ))}
                        {dropEnabled && rows.length > 0 && (
                          <div
                            className={`rail-drop-tail${
                              dropTarget?.projectId === project.id && dropTarget.before === null
                                ? ' active'
                                : ''
                            }`}
                            onDragOver={(e) => {
                              e.preventDefault();
                              e.dataTransfer.dropEffect = 'move';
                              setDropTarget({ projectId: project.id, before: null });
                            }}
                            onDrop={(e) => {
                              e.preventDefault();
                              handleDrop(project.id, null);
                            }}
                          >
                            放到最后
                          </div>
                        )}
                        {missing > 0 && !sessionsPending && (
                          <div
                            className="rail-project-missing"
                            title="账本里有这些会话 id，但会话日志不存在（已被删除或不属于本机）"
                          >
                            {missing} 条会话日志缺失
                          </div>
                        )}
                      </div>
                    )}
                  </section>
                );
              })}
            </>
          ) : (
            // 「还没有项目」在项目区为空时显示（UI-05 的空态文案 + 行动链接）。
            // projectsError 已单独兜住「列表请求失败」的情形（上方错误条），
            // 这里不再需要第二个条件——此前挂的 `!showEmpty` 恰好把真·空态
            // （sessions 与 projects 都为 0）下的文案整段吞掉，与文字按钮指涉
            // 自相矛盾，正是本票要消灭的断点。
            !projectsError && (
              <div className="rail-project-empty rail-project-empty-first">
                还没有项目。注册一个已存在的目录，同目录的会话就会归到一起。
                <button className="rail-empty-action" onClick={() => setCreateOpen(true)}>
                  注册项目目录 →
                </button>
              </div>
            )
          )}
        </section>

        {!sessionsPending && (
          <section className="rail-section" aria-label="未分组">
            <div className="rail-section-label">
              未分组
              <span className="rail-section-count num">{model.ungrouped.length}</span>
            </div>
            {model.ungrouped.length === 0 && projects.length > 0 && (
              <div className="rail-project-empty">所有会话都已归入项目。</div>
            )}
            <div>
              {model.ungrouped.map((s) => (
                <SessionRow
                  key={s.session_id}
                  session={s}
                  title={titlesById[s.session_id] ?? ''}
                  selected={selectedId === s.session_id}
                  live={liveSessionId === s.session_id}
                  now={now}
                  projectId={null}
                  ledger={[]}
                  /* #1028 AC3（**部分满足**，已知收缩）：仅就**可得**的 cwd（= 选中会话的
                     事件流）就地标注，历史未加载行不编路径——`SessionSummary` 本身不含
                     cwd，未选中的行此刻没有真值可标。故不是"每行都标"，而是"标得起的那一
                     行才标"；这是数据可得性自洽，不是缺陷，但如实登记在此以免被读成"每行
                     都带 cwd 标注"。 */
                  ownerCwd={selectedId === s.session_id ? selectedCwd : null}
                  dragging={false}
                  dropEnabled={false}
                  dropBefore={false}
                  onSelect={onSelect}
                  onMove={handleMove}
                  onDetach={() => undefined}
                  onAttachPick={setAttachFor}
                  onOpenCreate={() => setCreateOpen(true)}
                  onDragStartRow={() => undefined}
                  onDragEndRow={resetDrag}
                  onDragOverRow={() => undefined}
                  onDropRow={() => undefined}
                  onDelete={setDeletingSession}
                  onSetArchived={(sessionId, archived) =>
                    void handleSetArchived(sessionId, archived)
                  }
                  onReplay={setReplayTarget}
                />
              ))}
            </div>
          </section>
        )}
      </div>

      <CreateProjectDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        existing={projects}
        onCreate={async (path, title) => {
          const created = await projectActions.create(path, title);
          onSessionsChanged();
          return created;
        }}
      />
      <DeleteProjectDialog
        project={deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        onConfirm={async (projectId) => {
          const result = await projectActions.remove(projectId);
          onSessionsChanged();
          return result;
        }}
      />
      <DeleteSessionDialog
        target={deletingSession}
        onOpenChange={(open) => {
          if (!open) setDeletingSession(null);
        }}
        onConfirm={onDeleteSession}
      />
      {/* #1322：逻辑回放浮层。自包含（只读端点，不改任何会话状态），故不经过 App
          ——与硬删（要收敛列表状态）刻意不同。 */}
      <SessionReplayDialog
        target={replayTarget}
        onOpenChange={(open) => {
          if (!open) setReplayTarget(null);
        }}
      />
      <AttachToProjectDialog
        sessionId={attachFor}
        projects={projects}
        onOpenChange={(open) => {
          if (!open) setAttachFor(null);
        }}
        onPick={async (projectId, sessionId) => {
          await projectActions.attach(projectId, sessionId);
          onSessionsChanged();
        }}
      />
      <TaskCreationDialog
        project={startTaskFor}
        models={models}
        sandboxBackends={sandboxBackends}
        onOpenChange={(open) => {
          if (!open) setStartTaskFor(null);
        }}
        onCreateTask={onStartTask}
      />
    </aside>
  );
});

interface RowProps {
  session: UngroupedRow;
  title: string;
  selected: boolean;
  live: boolean;
  now: number;
  /** 所在项目 id；null = 未分组。 */
  projectId: string | null;
  /** 所在项目账本（判边界：首条不能上移、末条不能下移）；未分组传空数组。 */
  ledger: readonly string[];
  /** #1028：未归属且已选中时，用于就地标注的会话 cwd（绝对路径）。null/缺省 = 不标。 */
  ownerCwd?: string | null;
  dragging: boolean;
  /** 本行是否接受拖放（= 拖拽来源就在本项目）。跨项目拖动不是重排，不接受落点。 */
  dropEnabled: boolean;
  dropBefore: boolean;
  onSelect: (id: string) => void;
  onMove: (projectId: string, sessionId: string, dir: 'up' | 'down') => void;
  onDetach: (sessionId: string) => void;
  onAttachPick: (sessionId: string) => void;
  onOpenCreate: () => void;
  onDragStartRow: (sessionId: string) => void;
  onDragEndRow: () => void;
  /** 悬停到本行 → 落点为「本行之前」。 */
  onDragOverRow: (before: string) => void;
  /** 在本行落下 → 插入到本行之前。 */
  onDropRow: (before: string) => void;
  /** 打开本行的硬删确认面（真正删在前端最上层的 onDeleteSession 里）。 */
  onDelete: (target: DeleteSessionTarget) => void;
  /** 本行归档 / 取消归档（#171）。真正落库在 App 侧的 onSetArchived 里；
   *  本组件只转交意图，不碰会话真相。 */
  onSetArchived: (sessionId: string, archived: boolean) => void;
  /** 打开本行的重放浮层（`#1322`）。重放只读、零副作用，故只把意图转交出去
   *  （浮层自包含，真正调用在 `SessionReplayDialog` 里）。 */
  onReplay: (target: ReplaySessionTarget) => void;
}

function SessionRow({
  session: s,
  title,
  selected,
  live,
  now,
  projectId,
  ledger,
  ownerCwd,
  dragging,
  dropEnabled,
  dropBefore,
  onSelect,
  onMove,
  onDetach,
  onAttachPick,
  onOpenCreate,
  onDragStartRow,
  onDragEndRow,
  onDragOverRow,
  onDropRow,
  onDelete,
  onSetArchived,
  onReplay,
}: RowProps) {
  const inProject = projectId !== null;
  const index = ledger.indexOf(s.session_id);
  // #1028：本行的归属标注文案（有选中会话的 cwd 才算；纯函数，算一次供 title/label 复用）。
  const owner = ownerCwd != null ? describeOwnership(null, ownerCwd) : null;
  return (
    <div
      className={`session-row${inProject ? ' session-row-in-project' : ''}${dragging ? ' dragging' : ''}`}
      draggable={inProject}
      onDragStart={(e) => {
        if (!inProject) return;
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', s.session_id);
        onDragStartRow(s.session_id);
      }}
      onDragEnd={onDragEndRow}
      onDragOver={(e) => {
        if (!inProject || !dropEnabled || dragging) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        onDragOverRow(s.session_id);
      }}
      onDrop={(e) => {
        // 落点在行的上半/下半不做区分：统一「放到这一行前面」+ 队尾放置区，
        // 少一个模糊点，也少一类"以为放到后面、结果插到前面"的意外。
        if (!inProject || !dropEnabled) return;
        e.preventDefault();
        e.stopPropagation();
        onDropRow(s.session_id);
      }}
    >
      {dropBefore && <span className="rail-drop-indicator" aria-hidden="true" />}
      <button
        className={`session-item ${selected ? 'selected' : ''}`}
        onClick={() => onSelect(s.session_id)}
        title={`${s.session_id} · ${s.event_count} 事件${inProject ? '' : ' · 未分组'}`}
      >
        <span
          className={`session-item-dot ${live ? 'session-item-dot-live' : ''}`}
          aria-hidden="true"
        />
        <div className="session-item-body">
          {title && <div className="session-item-title">{title.slice(0, 48)}</div>}
          <div className="session-item-id mono">{s.session_id.slice(0, 12)}</div>
          <div className="session-item-meta num">
            {s.event_count} 事件 · {formatRelativeTime(s.last_event_time, now)}
          </div>
          {/* #171：「已归档」徽标。只有打开「显示已归档」时这一行才会被渲染出来
              （默认视图里已归档行整个不渲染，见 buildRailModel），所以徽标是**这一行
              此刻的唯一解释**：它不是被删了、也不是日志丢了，只是收起来了。 */}
          {s.archived && (
            <div
              className="session-item-archived"
              title="已归档：默认从列表收起，事件日志与项目归属都原样保留"
            >
              已归档
            </div>
          )}
          {/* #1028 AC3：未归属会话的 cwd 就地标注（归属警示）。只有**选中**的行
             才有已加载的 cwd（后端 `session/started.cwd` 随该会话事件流到来），
             其它未加载的行不编路径——"标得起才标"。 */}
          {owner != null && (
            <div
              className="session-item-owner"
              role="note"
              title={owner.title}
            >
              {owner.label}
            </div>
          )}
          {s.staleProject && (
            <div
              className="session-item-stale"
              title={`这个会话记录的项目 id 是 ${s.staleProject.id}，但它不在当前项目列表里——项目列表可能还没刷新，或该项目已被移除。归属仍以项目账本为准。`}
            >
              所属项目「{s.staleProject.title}」未在列表中
            </div>
          )}
          {/* #752：「已损坏」徽标。事件日志零可解析事件但有损坏行——损坏是
              可观测状态，不是"不存在"。点选后可用恢复入口（409 诊断）修复，
              而不是让会话静默消失。 */}
          {s.corrupted && (
            <div
              className="session-item-corrupted"
              title="事件日志损坏（零可解析事件）：点选后可通过恢复入口查看诊断并修复"
            >
              已损坏
            </div>
          )}
        </div>
      </button>
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <button
            className="icon-btn rail-menu-btn"
            aria-label={`会话 ${s.session_id.slice(0, 8)} 操作`}
          >
            <Ellipsis size={14} />
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content className="rail-menu" align="end" sideOffset={4}>
            {inProject ? (
              <>
                <DropdownMenu.Item
                  className="rail-menu-item"
                  disabled={index <= 0}
                  onSelect={() => projectId && onMove(projectId, s.session_id, 'up')}
                >
                  <ArrowUp size={12} aria-hidden="true" /> 上移
                </DropdownMenu.Item>
                <DropdownMenu.Item
                  className="rail-menu-item"
                  disabled={index < 0 || index >= ledger.length - 1}
                  onSelect={() => projectId && onMove(projectId, s.session_id, 'down')}
                >
                  <ArrowDown size={12} aria-hidden="true" /> 下移
                </DropdownMenu.Item>
                <DropdownMenu.Separator className="rail-menu-sep" />
                <DropdownMenu.Item className="rail-menu-item" onSelect={() => onDetach(s.session_id)}>
                  移出项目
                </DropdownMenu.Item>
              </>
            ) : (
              <>
                <DropdownMenu.Item
                  className="rail-menu-item"
                  onSelect={() => onAttachPick(s.session_id)}
                >
                  加入项目…
                </DropdownMenu.Item>
                <DropdownMenu.Item className="rail-menu-item" onSelect={onOpenCreate}>
                  新建项目…
                </DropdownMenu.Item>
              </>
            )}
            <DropdownMenu.Separator className="rail-menu-sep" />
            {/* #171：归档 / 取消归档（**可逆**，不是删除）。位置在项目动作之后、硬删
                之前——两者之间隔着一个分隔符与整整一组动作，是刻意的：点错"收起来"
                只是少看一行，点错"删掉"不可恢复。可逆动作**不做二次确认**（AC11），
                失败就地报错（`opError` 那条栏），撤销 = 再点一次「取消归档」。
                文案随行的真值切换（`s.archived`），不是两个按钮。 */}
            <DropdownMenu.Item
              className="rail-menu-item"
              onSelect={() => onSetArchived(s.session_id, !s.archived)}
            >
              {s.archived ? '取消归档' : '归档'}
            </DropdownMenu.Item>
            {/* #1322：逻辑回放入口（README 承诺的「可重放」在 Web 侧的唯一入口）。
                与归档同组（都是非破坏性动作），刻意排在硬删之前——重放只读、零
                副作用、可无限重来，离"删除"这道缓冲越远越好。二次确认在浮层里
                （讲清"零副作用地重放"，见 SessionReplayDialog）。 */}
            <DropdownMenu.Item
              className="rail-menu-item"
              onSelect={() => onReplay({ id: s.session_id, title })}
            >
              重放会话…
            </DropdownMenu.Item>
            <DropdownMenu.Separator className="rail-menu-sep" />
            {/* 硬删（#172 / ADR-0029）——两分支共用、永远最后一项：不可恢复的动作
                不夹在"上移/加入项目"中间，位置本身就是一道缓冲。
                刻意**不**对 live 行禁用：会话是否"忙"（在途 run / 挂起审批）只有
                后端知道，前端据 live 猜一个禁用态就是第二套真相（不变量 #22），
                猜错时留给用户的是一个点了没反应的灰按钮。让后端拒绝并显示它的
                detail（409 的三种原因，见 DeleteSessionDialog）。 */}
            <DropdownMenu.Item
              className="rail-menu-item rail-menu-item-danger"
              onSelect={() => onDelete({ id: s.session_id, title })}
            >
              删除会话…
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
    </div>
  );
}
