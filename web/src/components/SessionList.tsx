/** SessionList — Session Rail of the single-frame shell（Phase 2 Brief §10；WS-5 #155 改版）。
 *
 * 两层结构：
 *   项目（注册表顺序）→ 会话（**账本手工序**，用户拖出来的顺序，不按活动时间重排）
 *   未分组 → 不属于任何项目的会话（保持后端给的活动时间序）
 *
 *  三条不变量：
 *  1. **不丢行**：分组由 `lib/projects.ts` 的 `buildRailModel` 派生，未出现在任何账本
 *     里的会话（含 workspace 引用已失效项目的孤儿行）一律落进未分组区。
 *  2. **不改真相**：顺序与归属都来自后端（`GET /api/projects` 的 `session_ids`、
 *     `GET /api/sessions` 的 `workspace`）；本组件不维护影子成员名单，每次写操作后
 *     重新拉取（`ProjectActions` 内部 refresh + `onSessionsChanged`）。
 *  3. **失败要说话**：项目写操作失败时把后端 detail 原样贴在侧栏顶部（`rail-error`），
 *     而不是静默或只留一个禁用的按钮。
 *
 *  时间分组（Running/Today/Yesterday/Older）在 WS-5 后**只保留 Running 的活体语义**：
 *  行上的绿点 + 呼吸仍表示"该会话正在流式"，但不再用时间桶切分列表——侧栏现在有
 *  真实的层级要表达，两套切分叠在一起会让同一行出现在两个不同的分类逻辑下。
 */

import { memo, useMemo, useState } from 'react';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import {
  ArrowDown,
  ArrowUp,
  ChevronRight,
  Ellipsis,
  Folder,
  FolderOpen,
  FolderPlus,
  Plus,
  TriangleAlert,
} from 'lucide-react';
import type { Project, SessionSummary } from '../types';
import { formatRelativeTime } from '../lib/format';
import { useTickingNow } from '../hooks/useTickingNow';
import { buildRailModel, dropAnchor, moveAnchor } from '../lib/projects';
import { describeProjectError } from '../lib/api';
import type { ProjectActions } from '../hooks/useProjects';
import {
  AttachToProjectDialog,
  CreateProjectDialog,
  DeleteProjectDialog,
  InlineRename,
} from './ProjectDialogs';

interface Props {
  sessions: SessionSummary[];
  projects: Project[];
  selectedId: string | null;
  /** The session currently receiving a live stream (live dot). */
  liveSessionId: string | null;
  /** 行标题缓存（Session Model E 轮）：键为 session_id，值为首条 user/message 投影。
   *  无缓存的会话回退到短 ID——不伪造标题。 */
  titlesById: Record<string, string>;
  onSelect: (id: string) => void;
  onNew: () => void;
  projectActions: ProjectActions;
  /** 会话语义发生变化后刷新列表（attach/detach/软删除都会改各行的 `workspace`）。 */
  onSessionsChanged: () => void;
  /** `GET /api/projects` 失败原因（有值时项目区**不隐藏**，只是多一条可重试的错误条：
   *  隐藏项目会把所有会话误显示成未分组，比显示一条错误糟）。 */
  projectsError: string | null;
  onRetryProjects: () => void;
}

// memo：流式期间本组件 props（sessions/projects/selectedId/titlesById/回调）全部引用
// 稳定，跳过每个 delta 的会话栏重渲染；相对时间刷新由内部 useTickingNow 独立驱动。
export const SessionList = memo(function SessionList({
  sessions,
  projects,
  selectedId,
  liveSessionId,
  titlesById,
  onSelect,
  onNew,
  projectActions,
  onSessionsChanged,
  projectsError,
  onRetryProjects,
}: Props) {
  const now = useTickingNow();
  // 「项目有哪些会话」= 账本投影，不另存一份（不变量 #22）。
  const model = useMemo(() => buildRailModel(sessions, projects), [sessions, projects]);

  // 折叠状态是**视图**状态（不持久化，与 Inspector 折叠同一立场）。
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [createOpen, setCreateOpen] = useState(false);
  const [deleting, setDeleting] = useState<Project | null>(null);
  const [attachFor, setAttachFor] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [opError, setOpError] = useState<string | null>(null);
  // 拖拽重排（HTML5 DnD）：dragId = 被拖的会话；dropTarget = 落点（before=null → 队尾）。
  const [dragId, setDragId] = useState<string | null>(null);
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
    setDragId(null);
    setDropTarget(null);
  };

  /** 拖放落定：`before` = 放到这一行前面，null = 放到该项目队尾。 */
  const handleDrop = (projectId: string, before: string | null) => {
    const sessionId = dragId;
    resetDrag();
    if (!sessionId) return;
    const ids = ledgerOf(projectId);
    // 只支持**项目内**拖动重排：未分组的行拖到项目上等于 attach，而 attach 有
    // cwd 校验（可能 409）——那条路径走"加入项目…"，这里不猜。
    if (!ids.includes(sessionId)) return;
    const anchor = dropAnchor(ids, sessionId, before);
    if (!anchor) return;
    void runOp('调整顺序失败', () => projectActions.reorder(projectId, sessionId, anchor.before));
  };

  const showEmpty = sessions.length === 0 && projects.length === 0;
  const draggingInProject = dragId !== null && projects.some((p) => p.session_ids.includes(dragId));

  return (
    <aside className="session-rail">
      <div className="session-list-header">
        <span className="panel-label">会话</span>
        <div className="session-list-actions">
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
      </div>

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
      ) : projectsError ? (
        <div className="rail-error" role="alert">
          <span className="rail-error-text" title={projectsError}>
            项目列表加载失败：{projectsError}
          </span>
          <button className="rail-error-retry" onClick={onRetryProjects}>
            重试
          </button>
        </div>
      ) : null}

      <div className="session-items">
        {showEmpty && <div className="empty-hint">暂无会话，提交任务即可开始。</div>}

        <section className="rail-section" aria-label="项目">
          {projects.length > 0 ? (
            <>
              <div className="rail-section-label">项目</div>
              {model.groups.map(({ project, sessions: rows, missing }) => {
                const expanded = isExpanded(project);
                const listId = `rail-project-${project.id}`;
                const renaming = renamingId === project.id;
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
                          className="rail-project-toggle"
                          onClick={() => toggle(project.id)}
                          aria-expanded={expanded}
                          aria-controls={listId}
                          title={expanded ? '收起项目' : `展开项目（${rows.length} 个会话）`}
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
                          {project.status === 'missing-dir' && (
                            <TriangleAlert
                              size={12}
                              className="rail-project-warn"
                              role="img"
                              aria-label="目录当前不存在"
                            />
                          )}
                          <span className="rail-project-count num">{rows.length}</span>
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
                          <DropdownMenu.Content className="rail-menu" align="end" sideOffset={4}>
                            <DropdownMenu.Item
                              className="rail-menu-item"
                              onSelect={() => setRenamingId(project.id)}
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
                      <div className="rail-project-rows" role="list" id={listId}>
                        {rows.length === 0 && (
                          <div className="rail-project-empty">
                            还没有会话。在该目录下新建会话，或从未分组会话的「加入项目…」里选它。
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
                            dragging={dragId === s.session_id}
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
                            onDragStartRow={setDragId}
                            onDragEndRow={resetDrag}
                            onDragOverRow={(before) =>
                              setDropTarget({ projectId: project.id, before })
                            }
                            onDropRow={(before) => handleDrop(project.id, before)}
                          />
                        ))}
                        {draggingInProject && dragId && rows.length > 0 && (
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
                        {missing > 0 && (
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
            !showEmpty && (
              <div className="rail-project-empty rail-project-empty-first">
                还没有项目。用右上角的「新建项目」把一个已存在的目录注册进来，同目录的会话就会归到一起。
              </div>
            )
          )}
        </section>

        <section className="rail-section" aria-label="未分组">
          <div className="rail-section-label">
            未分组
            <span className="rail-section-count num">{model.ungrouped.length}</span>
          </div>
          {model.ungrouped.length === 0 && projects.length > 0 && (
            <div className="rail-project-empty">所有会话都已归入项目。</div>
          )}
          <div role="list">
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
                dragging={false}
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
              />
            ))}
          </div>
        </section>
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
    </aside>
  );
});

interface RowProps {
  session: SessionSummary;
  title: string;
  selected: boolean;
  live: boolean;
  now: number;
  /** 所在项目 id；null = 未分组。 */
  projectId: string | null;
  /** 所在项目账本（判边界：首条不能上移、末条不能下移）；未分组传空数组。 */
  ledger: readonly string[];
  dragging: boolean;
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
}

function SessionRow({
  session: s,
  title,
  selected,
  live,
  now,
  projectId,
  ledger,
  dragging,
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
}: RowProps) {
  const inProject = projectId !== null;
  const index = ledger.indexOf(s.session_id);
  return (
    <div
      role="listitem"
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
        if (!inProject || dragging) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        onDragOverRow(s.session_id);
      }}
      onDrop={(e) => {
        // 落点在行的上半/下半不做区分：统一「放到这一行前面」+ 队尾放置区，
        // 少一个模糊点，也少一类"以为放到后面、结果插到前面"的意外。
        if (!inProject) return;
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
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
    </div>
  );
}
