import { Fragment, useEffect, useRef, useState, type FormEvent } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Brain, ChevronDown, ChevronUp, RefreshCw, Search, Trash2, X } from 'lucide-react';
import { describeMemoryError, listProjects, MemoryError } from '../lib/api';
import {
  bulkDeleteMemoryRecords,
  countMemoryRoots,
  editMemoryRecord,
  getMemoryVersions,
  getMemorySettings,
  updateMemorySettings,
  type MemoryKind,
  type MemoryRecord,
  type MemorySettings,
  type MemoryStatus,
  type MemoryScopeV2,
  type MemoryFilters,
} from '../lib/memoryV2Api';
import type { Project } from '../types';
import {
  formatMemoryTime,
  MEMORY_CONTENT_MAX_CHARS,
  MEMORY_PAYLOAD_FIELDS,
  memoryPayloadDraft,
  memoryPayloadFromDraft,
  SEMANTIC_CATEGORIES,
  validateMemoryEdit,
} from '../lib/memory';
import { useMemories } from '../hooks/useMemories';
import '../styles/memory-v2.css';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const KIND_LABELS: Record<MemoryKind, string> = {
  semantic: '语义', episodic: '情景', procedural: '程序',
};
const STATUS_LABELS: Record<MemoryStatus, string> = {
  active: '有效', superseded: '已替代', invalidated: '已失效', deleted: '已删除',
};
const SCOPE_LABELS: Record<MemoryScopeV2, string> = {
  user_global: '全局', project: '项目',
};
const CONTENT_CLAMP_THRESHOLD = 180;

export function MemoryPanel({ open, onOpenChange }: Props) {
  const openerRef = useRef<HTMLElement | null>(null);
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        {open && <MemoryPanelBody openerRef={openerRef} />}
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function MemoryPanelBody({ openerRef }: { openerRef: { current: HTMLElement | null } }) {
  const [queryDraft, setQueryDraft] = useState('');
  const [filters, setFilters] = useState<MemoryFilters>({});
  const memories = useMemories(filters);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [settings, setSettings] = useState<MemorySettings | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [settingsLoading, setSettingsLoading] = useState(true);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [bulkKind, setBulkKind] = useState<MemoryKind | ''>('');
  const [bulkPreview, setBulkPreview] = useState<{ count: number; exact: boolean; kind: MemoryKind | null; projectId?: string; projectTitle: string | null } | null>(null);
  const [bulkToken, setBulkToken] = useState('');
  const [bulkBusy, setBulkBusy] = useState(false);

  useEffect(() => {
    let current = true;
    void getMemorySettings().then((value) => {
      if (current) {
        setSettings(value);
        setSettingsError(null);
      }
    }).catch((error: unknown) => {
      if (current) setSettingsError(describeMemoryError(error, '加载记忆设置失败'));
    }).finally(() => {
      if (current) setSettingsLoading(false);
    });
    return () => { current = false; };
  }, []);

  useEffect(() => {
    let current = true;
    void listProjects().then((value) => {
      if (current) {
        setProjects(value);
        setProjectsError(null);
      }
    }).catch((error: unknown) => {
      if (current) setProjectsError(describeMemoryError(error, '加载项目列表失败'));
    }).finally(() => {
      if (current) setProjectsLoading(false);
    });
    return () => { current = false; };
  }, []);

  const retryProjects = async () => {
    setProjectsLoading(true);
    setProjectsError(null);
    try { setProjects(await listProjects()); }
    catch (error) { setProjectsError(describeMemoryError(error, '加载项目列表失败')); }
    finally { setProjectsLoading(false); }
  };

  const saveSetting = async (key: keyof MemorySettings, value: boolean) => {
    if (settingsBusy || !settings) return;
    setSettingsBusy(true);
    setSettingsError(null);
    try {
      setSettings(await updateMemorySettings({ [key]: value }));
    } catch (error) {
      setSettingsError(describeMemoryError(error, '保存记忆设置失败'));
    } finally {
      setSettingsBusy(false);
    }
  };

  const retrySettings = async () => {
    setSettingsLoading(true);
    setSettingsError(null);
    try { setSettings(await getMemorySettings()); }
    catch (error) { setSettingsError(describeMemoryError(error, '加载记忆设置失败')); }
    finally { setSettingsLoading(false); }
  };

  const applyFilters = () => {
    const q = queryDraft.trim();
    setFilters((previous) => ({ ...previous, ...(q ? { q } : { q: undefined }) }));
  };

  const clearFilters = () => {
    setQueryDraft('');
    setFilters({});
  };

  const previewBulkDelete = async () => {
    setPanelError(null);
    setBulkPreview(null);
    setBulkToken('');
    const kind = bulkKind || null;
    try {
      const projectId = filters.project_id;
      setBulkPreview({
        ...await countMemoryRoots(kind ?? undefined, projectId),
        kind,
        projectId,
        projectTitle: projects.find((project) => project.id === projectId)?.title ?? null,
      });
    } catch (error) {
      setPanelError(describeMemoryError(error, '读取待删除数量失败'));
    }
  };

  const confirmBulkDelete = async () => {
    if (!bulkPreview?.exact || bulkToken !== 'DELETE' || bulkBusy) return;
    setBulkBusy(true);
    setPanelError(null);
    try {
      const result = await bulkDeleteMemoryRecords(bulkPreview.kind, bulkPreview.projectId);
      setPanelError(`已删除 ${result.affected_count} 条记忆。`);
      setBulkPreview(null);
      setBulkToken('');
    } catch (error) {
      setPanelError(describeMemoryError(error, '批量删除记忆失败'));
    } finally {
      await memories.retry();
      setBulkBusy(false);
    }
  };

  return (
    <Dialog.Content
      className="memory-panel memory-v2-panel"
      onOpenAutoFocus={() => {
        openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      }}
      onCloseAutoFocus={(event) => {
        if (openerRef.current?.isConnected) {
          event.preventDefault();
          openerRef.current.focus();
        }
      }}
    >
      <div className="project-dialog-head">
        <Dialog.Title className="project-dialog-title">
          <Brain size={14} aria-hidden="true" /> 记忆管理
        </Dialog.Title>
        <Dialog.Close asChild>
          <button className="icon-btn project-dialog-close" aria-label="关闭"><X size={14} /></button>
        </Dialog.Close>
      </div>
      <Dialog.Description className="project-dialog-desc">
        管理长期记忆。编辑会生成新版本；删除不可恢复。列表与设置以服务端返回为准。
      </Dialog.Description>

      <div className="memory-v2-settings" aria-label="记忆设置">
        <span className="memory-v2-section-title">自动化</span>
        {settingsLoading ? <span role="status">正在读取设置…</span> : settings && (
          <>
            <label><input type="checkbox" checked={settings.extraction_enabled} disabled={settingsBusy} onChange={(event) => void saveSetting('extraction_enabled', event.target.checked)} /> 自动提取</label>
            <label><input type="checkbox" checked={settings.recall_enabled} disabled={settingsBusy} onChange={(event) => void saveSetting('recall_enabled', event.target.checked)} /> 自动召回</label>
          </>
        )}
        {settingsError && <span className="memory-v2-error" role="alert">{settingsError}<button className="memory-v2-quiet" disabled={settingsLoading} onClick={() => void retrySettings()}>重试</button></span>}
      </div>

      <form className="memory-v2-filters" onSubmit={(event) => { event.preventDefault(); applyFilters(); }}>
        <label><span className="sr-only">项目范围</span><select
          aria-label="项目范围"
          value={filters.project_id ?? ''}
          disabled={projectsLoading || projectsError !== null}
          onChange={(event) => {
            const projectId = event.target.value || undefined;
            setFilters((prev) => ({ ...prev, project_id: projectId, scope: projectId ? 'project' : undefined }));
            setBulkPreview(null);
            setBulkToken('');
          }}
        >
          <option value="">全局记忆</option>
          {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
        </select></label>
        <label className="memory-v2-search">
          <span className="sr-only">搜索记忆</span>
          <input value={queryDraft} onChange={(event) => setQueryDraft(event.target.value)} placeholder="搜索记忆" />
          <button type="submit" aria-label="搜索"><Search size={14} /></button>
        </label>
        <label><span className="sr-only">类型</span><select aria-label="类型" value={filters.kind ?? ''} onChange={(event) => setFilters((prev) => ({ ...prev, kind: (event.target.value || undefined) as MemoryKind | undefined }))}>
          <option value="">全部类型</option><option value="semantic">语义</option><option value="episodic">情景</option><option value="procedural">程序</option>
        </select></label>
        <label><span className="sr-only">状态</span><select aria-label="状态" value={filters.status ?? ''} onChange={(event) => setFilters((prev) => ({ ...prev, status: (event.target.value || undefined) as MemoryStatus | undefined }))}>
          <option value="">全部状态</option><option value="active">有效</option><option value="superseded">已替代</option><option value="invalidated">已失效</option><option value="deleted">已删除</option>
        </select></label>
        <label><span className="sr-only">范围</span><select aria-label="范围" value={filters.scope ?? ''} onChange={(event) => setFilters((prev) => ({ ...prev, scope: (event.target.value || undefined) as MemoryScopeV2 | undefined }))}>
          <option value="">全部范围</option><option value="user_global" disabled={Boolean(filters.project_id)}>全局</option><option value="project">项目</option>
        </select></label>
        <button type="button" className="memory-v2-quiet" onClick={clearFilters}>清除筛选</button>
        <button type="button" className="memory-v2-quiet" onClick={() => void memories.retry()} aria-label="刷新记忆"><RefreshCw size={14} /> 刷新</button>
      </form>
      {projectsError && <div className="memory-v2-error" role="alert">{projectsError}<button className="memory-v2-quiet" disabled={projectsLoading} onClick={() => void retryProjects()}>重试</button></div>}
      {filters.scope === 'project' && !filters.project_id && <p className="memory-v2-hint">选择项目后查看该项目的记忆。</p>}

      <section className="memory-v2-bulk" aria-label="批量删除">
        <label>批量删除
          <select aria-label="批量删除类型" value={bulkKind} onChange={(event) => { setBulkKind(event.target.value as MemoryKind | ''); setBulkPreview(null); setBulkToken(''); }}>
            <option value="">全部类型</option><option value="semantic">语义</option><option value="episodic">情景</option><option value="procedural">程序</option>
          </select>
        </label>
        <button type="button" className="memory-v2-danger" onClick={() => void previewBulkDelete()}>预览删除</button>
        {bulkPreview && <div className="memory-v2-confirm" role="group" aria-label="确认批量删除">
          <p>{bulkPreview.exact
            ? `将永久删除${bulkPreview.projectTitle ? `全局和“${bulkPreview.projectTitle}”项目中` : '全局范围内'} ${bulkPreview.count} 条${bulkPreview.kind ? `${KIND_LABELS[bulkPreview.kind]}类` : ''}记忆。`
            : `记录超过可精确统计上限（已确认至少 ${bulkPreview.count} 条），无法安全批量删除。`}</p>
          {bulkPreview.exact && bulkPreview.count > 0 && <>
            <label>输入 DELETE 确认<input aria-label="输入 DELETE 确认" value={bulkToken} onChange={(event) => setBulkToken(event.target.value)} autoComplete="off" /></label>
            <button type="button" className="memory-v2-danger" disabled={bulkBusy || bulkToken !== 'DELETE'} onClick={() => void confirmBulkDelete()}>{bulkBusy ? '删除中…' : '永久删除'}</button>
          </>}
          <button type="button" className="memory-v2-quiet" onClick={() => setBulkPreview(null)}>取消</button>
        </div>}
      </section>

      <div className="memory-v2-body">
        {memories.disabled && <div className="memory-degraded" role="status"><strong>记忆未启用</strong><span>{memories.disabled}</span></div>}
        {memories.loading && <div className="memory-loading" role="status">正在加载记忆…</div>}
        {!memories.loading && !memories.disabled && memories.visible.length === 0 && !memories.loadError && (
          <div className="memory-empty" role="status">还没有记忆或符合条件的记录。</div>
        )}
        {memories.visible.length > 0 && <ul className="memory-list memory-v2-list">
          {memories.visible.map((record) => <MemoryRow
            key={record.id}
            record={record}
            pending={memories.pending.has(record.id)}
            onDelete={() => memories.remove(record.id, record.project_id ?? undefined)}
            onDeleted={() => setPanelError('已删除 1 条记忆。')}
            onNotice={setPanelError}
            onChanged={() => void memories.retry()}
          />)}
        </ul>}
        {memories.loadError && <div className="memory-error" role="alert"><span>{memories.loadError}</span><button onClick={() => void memories.retry()}>重试</button></div>}
        {panelError && <div className="memory-v2-error memory-error" role="status">{panelError}<button className="memory-v2-quiet" onClick={() => setPanelError(null)}>知道了</button></div>}
        {!memories.loading && memories.visible.length > 0 && (memories.hasMore
          ? <button className="memory-more-btn" disabled={memories.loadingMore} onClick={() => void memories.loadMore()}>{memories.loadingMore ? '加载中…' : '加载更多'}</button>
          : <span className="memory-more-end">已全部加载</span>)}
      </div>

      <div className="project-dialog-actions"><Dialog.Close asChild><button className="project-btn project-btn-primary">关闭</button></Dialog.Close></div>
    </Dialog.Content>
  );
}

function MemoryRow({ record, pending, onDelete, onDeleted, onNotice, onChanged }: {
  record: MemoryRecord;
  pending: boolean;
  onDelete: () => Promise<void>;
  onDeleted: () => void;
  onNotice: (message: string) => void;
  onChanged: () => void;
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [versions, setVersions] = useState<MemoryRecord[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(record.content);
  const [payloadDraft, setPayloadDraft] = useState(() => record.payload ? memoryPayloadDraft(record.payload) : {});
  const [editError, setEditError] = useState<string | null>(null);
  const [editBusy, setEditBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [contentExpanded, setContentExpanded] = useState(false);
  const kind = record.kind;
  const canEdit = kind !== null && record.status === 'active' && record.version !== null && record.payload !== null;
  const longContent = record.status !== 'deleted' && record.content.length > CONTENT_CLAMP_THRESHOLD;
  const showFullContent = !longContent || contentExpanded;

  const loadHistory = async () => {
    setVersions(null);
    setHistoryError(null);
    try { setVersions(await getMemoryVersions(record.id, record.project_id ?? undefined)); }
    catch (error) { setHistoryError(describeMemoryError(error, '加载版本历史失败')); }
  };

  const toggleHistory = async () => {
    if (historyOpen) { setHistoryOpen(false); return; }
    setHistoryOpen(true);
    await loadHistory();
  };

  const saveEdit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!kind || record.version === null) return;
    const payload = memoryPayloadFromDraft(kind, payloadDraft);
    const errors = validateMemoryEdit(record, content, payload);
    if (errors.length) { setEditError(errors.join(' ')); return; }
    setEditBusy(true);
    setEditError(null);
    try {
      await editMemoryRecord(record.id, { expected_version: record.version, content: content.trim(), payload: payload! }, record.project_id ?? undefined);
      setEditing(false);
      onChanged();
    } catch (error) {
      const message = error instanceof MemoryError && (error.status === 404 || error.status === 409)
        ? '记忆已被其他操作更新或删除。已刷新当前列表，请重新打开后再编辑。'
        : describeMemoryError(error, '保存记忆失败');
      setEditError(message);
      if (error instanceof MemoryError && (error.status === 404 || error.status === 409)) {
        onNotice(message);
        onChanged();
      }
    } finally { setEditBusy(false); }
  };

  const deleteRecord = async () => {
    setDeleteError(null);
    try { await onDelete(); setConfirmDelete(false); onDeleted(); }
    catch (error) {
      const message = describeMemoryError(error, '删除记忆失败');
      setDeleteError(message);
      if (error instanceof MemoryError && error.status === 404) onNotice(message);
    }
  };

  const editFields = kind ? MEMORY_PAYLOAD_FIELDS[kind] : [];
  return (
    <li className="memory-row memory-v2-row">
      <div className="memory-row-head">
        <p className={`memory-content${showFullContent ? '' : ' clamped'}`} title={record.status === 'deleted' ? undefined : record.content}>
          {record.status === 'deleted' ? '内容已删除' : record.content}
        </p>
        {longContent && <button className="memory-expand" aria-expanded={contentExpanded} aria-label={contentExpanded ? '收起全文' : '展开全文'} onClick={() => setContentExpanded((value) => !value)}>{contentExpanded ? '收起' : '展开全文'}</button>}
        <button className="memory-expand" aria-expanded={detailsOpen} onClick={() => setDetailsOpen((value) => !value)}>{detailsOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}<span>{detailsOpen ? '收起详情' : '详细信息'}</span></button>
      </div>
      <div className="memory-v2-meta">
        {record.kind && <span>{KIND_LABELS[record.kind]}</span>}
        {record.status && <span>{STATUS_LABELS[record.status]}</span>}
        {record.scope in SCOPE_LABELS && <span>{SCOPE_LABELS[record.scope as MemoryScopeV2]}</span>}
        {record.version !== null && <span>v{record.version}</span>}
        {record.tier && <span>{record.tier === 'profile' ? '画像' : '集合'}</span>}
        {record.source_type && <span>{record.source_type}</span>}
        <span className={`memory-scope${record.scope === 'session' ? ' scope-session' : ' scope-user'}`}>{record.scope === 'session' ? '会话' : record.scope === 'user' ? '用户' : SCOPE_LABELS[record.scope as MemoryScopeV2]}</span>
        <time className="memory-time" dateTime={record.deleted_at ?? record.created_at}>{formatMemoryTime(record.deleted_at ?? record.created_at)}</time>
        {kind === null && !record.is_tombstone && <span>旧版记忆</span>}
      </div>
      {detailsOpen && <dl className="memory-v2-details">
        {record.is_tombstone
          ? <><dt>删除时间</dt><dd>{formatMemoryTime(record.deleted_at ?? record.created_at)}</dd></>
          : <><dt>创建时间</dt><dd>{formatMemoryTime(record.created_at)}</dd><dt>来源</dt><dd>{record.source_type ?? '旧版'}</dd></>}
        {record.project_id && <><dt>项目 ID</dt><dd>{record.project_id}</dd></>}
        {!record.is_tombstone && record.source_session_id && <><dt>来源会话</dt><dd>{record.source_session_id}</dd></>}
        {!record.is_tombstone && record.source_event_ids.length > 0 && <><dt>来源事件</dt><dd>{record.source_event_ids.join(', ')}</dd></>}
        {!record.is_tombstone && record.payload && Object.entries(record.payload).filter(([key]) => key !== 'kind').map(([key, value]) => <Fragment key={key}><dt>{key === 'category' ? '类别' : key}</dt><dd>{value}</dd></Fragment>)}
      </dl>}

      {editing && kind && <form className="memory-v2-edit" onSubmit={(event) => void saveEdit(event)}>
        <label>记忆正文<textarea aria-label="记忆正文" value={content} onChange={(event) => setContent(event.target.value)} /></label>
        {editFields.map(({ key, label }) => <label key={key}>{label}<textarea aria-label={label} value={payloadDraft[key] ?? ''} onChange={(event) => setPayloadDraft((previous) => ({ ...previous, [key]: event.target.value }))} /></label>)}
        {kind === 'semantic' && <label>语义类别<select aria-label="语义类别" value={payloadDraft.category ?? ''} onChange={(event) => setPayloadDraft((previous) => ({ ...previous, category: event.target.value }))}><option value="">选择类别</option>{SEMANTIC_CATEGORIES.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}</select></label>}
        <span className="memory-v2-hint">每项最多 {MEMORY_CONTENT_MAX_CHARS} 个字符。</span>
        {editError && <span className="memory-v2-error" role="alert">{editError}</span>}
        <div className="memory-v2-actions"><button type="submit" disabled={editBusy}>{editBusy ? '保存中…' : '保存新版本'}</button><button type="button" className="memory-v2-quiet" onClick={() => { setEditing(false); setEditError(null); }}>取消</button></div>
      </form>}

      <div className="memory-v2-actions">
        {canEdit && !editing && <button onClick={() => { setContent(record.content); setPayloadDraft(memoryPayloadDraft(record.payload!)); setEditing(true); }}>编辑</button>}
        {record.root_id && <button className="memory-v2-quiet" onClick={() => void toggleHistory()}>{historyOpen ? '收起版本历史' : '版本历史'}</button>}
        {record.status !== 'deleted' && <button className="memory-v2-quiet" aria-label="删除这条记忆" disabled={pending} onClick={() => { setDeleteError(null); setConfirmDelete((value) => !value); }}><Trash2 size={13} /> 删除</button>}
      </div>
      {historyOpen && <div className="memory-v2-history" aria-label="版本历史">
        {historyError && <span className="memory-v2-error" role="alert">{historyError}<button onClick={() => void loadHistory()}>重试</button></span>}
        {versions === null && !historyError && <span role="status">正在读取版本…</span>}
        {versions?.map((version) => <div className="memory-v2-version" key={version.id}>
          <strong>{version.is_tombstone ? STATUS_LABELS.deleted : `v${version.version ?? '—'} · ${version.status ? STATUS_LABELS[version.status] : '旧版'}`}</strong>
          <time>{formatMemoryTime(version.created_at)}</time>
          <p>{version.status === 'deleted' ? '内容已删除' : version.content}</p>
        </div>)}
      </div>}
      {confirmDelete && <div className="memory-v2-confirm memory-confirm" role="group" aria-label="确认删除记忆">
        <span>永久删除这条记忆。删除不可恢复（没有回收站，也没有恢复入口）。</span><button className="memory-v2-danger" disabled={pending} onClick={() => void deleteRecord()}>{pending ? '删除中…' : '确认删除'}</button><button className="memory-v2-quiet" onClick={() => setConfirmDelete(false)}>取消</button>
      </div>}
      {deleteError && <span className="memory-v2-error project-error" role="alert">{deleteError}</span>}
    </li>
  );
}
