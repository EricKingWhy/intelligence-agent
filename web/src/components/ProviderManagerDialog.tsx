/** 自定义模型供应商管理弹层（#203 / ADR-0032 §8，图1 = dsh 供应商管理）。
 *
 *  结构（ADR-0032 §8.1）：两栏——左 = provider 列表（状态点 + 名称 + 标记 +
 *  上次测试）+ 底部「+ 新建供应商」；右 = 所选 provider 的表单（显示名 /
 *  Base URL / 模型列表 / API Key / 测试连接 / 保存 / 删除供应商）。
 *
 *  安全约束（ADR-0032 §7.1，前端侧职责）：
 *  - API Key 输入 `type="password"`；已有凭据时占位符「已保存（不回显）」——
 *    后端**永不**回传 key，前端也不显示任何片段；
 *  - 留空 = 不改（**不是**清除）；清除是独立动作（「清除已保存的密钥」+ 二次确认）；
 *  - 列表状态以 `has_api_key` 为准（后端真相，前端不维护第二套——不变量 #22）。
 *
 *  交互约束（§8.2）：保存不自动触发测试（用户裁定测试是显式动作）；删除二次
 *  确认（确认框内写明 provider id）；表单状态放在只在打开时挂载的内层组件里
 *  （ProjectDialogs 结构纪律：关闭即丢弃，上次的错误不漏到下一次）。
 */

import { useEffect, useMemo, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import {
  CircleDot, Eye, EyeOff, Plus, Trash2, TriangleAlert, X,
} from 'lucide-react';
import {
  createModelProvider,
  deleteModelProvider,
  describeSessionError as describeError,
  getModelProviders,
  testModelProvider,
  updateModelProvider,
  type ModelProviderEntry,
  type ProviderTestResult,
} from '../lib/api';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** provider 列表行的标记文案（ADR-0032 §3.2：UI 必须能区分三种来源）。 */
function kindBadge(kind: ModelProviderEntry['kind']): string {
  return kind === 'override' ? '已覆盖' : '自定义';
}

/** 状态点 aria-label（§8.2：状态点有 aria-label）。 */
function statusLabel(entry: ModelProviderEntry): string {
  return entry.is_available ? '已配置' : '未配置';
}

export function ProviderManagerDialog({ open, onOpenChange }: Props) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        {open && <ProviderManagerBody />}
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** 表单状态内层组件：只在打开时挂载（ProjectDialogs 结构纪律）。 */
function ProviderManagerBody() {
  const [providers, setProviders] = useState<ModelProviderEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const reload = async () => {
    setLoadError(null);
    try {
      const entries = await getModelProviders();
      setProviders(entries);
      // 当前选中项被删 → 回列表首位（没有则空面板）。
      setSelectedId((current) =>
        current && entries.some((e) => e.id === current) ? current : (entries[0]?.id ?? null),
      );
    } catch (e) {
      setLoadError(describeError(e, '加载供应商列表失败'));
    }
  };

  useEffect(() => {
    void reload();
    // 仅挂载时拉一次；保存/删除后的刷新由各动作自己 reload（不写进依赖）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selected = useMemo(
    () => providers?.find((p) => p.id === selectedId) ?? null,
    [providers, selectedId],
  );

  const select = (id: string) => {
    setSelectedId(id);
    setCreating(false);
  };

  return (
    <Dialog.Content className="provider-dialog" aria-describedby={undefined}>
      <div className="provider-dialog-head">
        <Dialog.Title className="provider-dialog-title">管理模型供应商</Dialog.Title>
        <Dialog.Close asChild>
          <button className="icon-btn project-dialog-close" aria-label="关闭">
            <X size={14} />
          </button>
        </Dialog.Close>
      </div>
      <div className="provider-dialog-columns">
        {/* 左栏：provider 列表 */}
        <div className="provider-list">
          {providers === null && !loadError && <div className="provider-list-empty">加载中…</div>}
          {loadError && <div className="project-error" role="alert">{loadError}</div>}
          {providers !== null && providers.length === 0 && !creating && (
            <div className="provider-list-empty">
              还没有自定义供应商。添加一个后，它的模型会出现在模型选择器里。
            </div>
          )}
          {providers?.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className={`provider-item${entry.id === selectedId && !creating ? ' sel' : ''}`}
              onClick={() => select(entry.id)}
            >
              <CircleDot
                size={10}
                className={entry.is_available ? 'provider-dot ok' : 'provider-dot off'}
                aria-label={statusLabel(entry)}
                role="img"
              />
              <span className="provider-item-name">{entry.label || entry.id}</span>
              <span className="provider-item-badge">{kindBadge(entry.kind)}</span>
            </button>
          ))}
          <button
            type="button"
            className={`provider-item provider-new${creating ? ' sel' : ''}`}
            onClick={() => { setCreating(true); setSelectedId(null); }}
          >
            <Plus size={12} aria-hidden="true" /> 添加供应商
          </button>
        </div>
        {/* 右栏：表单（编辑所选 / 新建） */}
        <div className="provider-form-pane">
          {creating ? (
            <ProviderForm
              key="new"
              mode="create"
              onSaved={() => { setCreating(false); void reload(); }}
              onDiscard={() => setCreating(false)}
            />
          ) : selected ? (
            <ProviderForm
              key={selected.id}
              mode="edit"
              entry={selected}
              onSaved={() => void reload()}
              onDeleted={() => void reload()}
            />
          ) : (
            <div className="provider-form-empty">从左侧选择一个供应商</div>
          )}
        </div>
      </div>
    </Dialog.Content>
  );
}

/** 表单（右栏）：create（新建）与 edit（编辑所选）共用一份实现。 */
function ProviderForm({
  mode,
  entry,
  onSaved,
  onDeleted,
  onDiscard,
}: {
  mode: 'create' | 'edit';
  entry?: ModelProviderEntry;
  onSaved: () => void;
  onDeleted?: () => void;
  onDiscard?: () => void;
}) {
  const [id, setId] = useState(entry?.id ?? '');
  const [label, setLabel] = useState(entry?.label ?? '');
  const [baseUrl, setBaseUrl] = useState(entry?.base_url ?? '');
  const [modelId, setModelId] = useState(entry?.models[0]?.model_id ?? '');
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [testResult, setTestResult] = useState<ProviderTestResult | null>(null);
  const [testPending, setTestPending] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingClearKey, setConfirmingClearKey] = useState(false);

  const isEdit = mode === 'edit';
  const hasSavedKey = entry?.has_api_key ?? false;

  const save = async () => {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      const payload = {
        base_url: baseUrl,
        models: modelId.trim() ? [{ model_id: modelId.trim() }] : [],
        // 留空 = 不改（不是清除）；仅填写了新值才发送（ADR-0032 §8.2）。
        ...(apiKey ? { api_key: apiKey } : {}),
        ...(isEdit ? {} : { api_key: apiKey }),
      };
      if (isEdit) {
        await updateModelProvider(entry!.id, payload);
      } else {
        await createModelProvider({ id: id.trim(), label, ...payload });
      }
      setApiKey('');
      onSaved();
    } catch (e) {
      setError(describeError(e, '保存失败'));
    } finally {
      setPending(false);
    }
  };

  const runTest = async () => {
    if (testPending || !isEdit) return;
    setTestPending(true);
    setTestResult(null);
    try {
      setTestResult(await testModelProvider(entry!.id));
    } catch (e) {
      setTestResult({ ok: false, message: describeError(e, '测试失败'), duration_ms: 0 });
    } finally {
      setTestPending(false);
    }
  };

  const doDelete = async () => {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      await deleteModelProvider(entry!.id);
      onDeleted?.();
    } catch (e) {
      setError(describeError(e, '删除失败'));
      setConfirmingDelete(false);
    } finally {
      setPending(false);
    }
  };

  const clearKey = async () => {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      // 显式清除：api_key="" 是独立动作（不是"顺手清"，§7.1 第 2 条）。
      await updateModelProvider(entry!.id, {
        base_url: baseUrl, models: entry!.models, api_key: '',
      });
      setConfirmingClearKey(false);
      onSaved();
    } catch (e) {
      setError(describeError(e, '清除密钥失败'));
      setConfirmingClearKey(false);
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="provider-form">
      <div className="provider-field">
        <label className="project-field-label" htmlFor="provider-id">ID</label>
        <input
          id="provider-id"
          className={`project-input${isEdit ? '' : ' mono'}`}
          value={id}
          onChange={(e) => setId(e.target.value)}
          disabled={isEdit}
          placeholder="小写字母/数字/连字符，如 my-proxy"
        />
        {isEdit && <div className="provider-field-hint">ID 创建后不可修改</div>}
      </div>
      <div className="provider-field">
        <label className="project-field-label" htmlFor="provider-label">显示名</label>
        <input
          id="provider-label"
          className="project-input"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="可选"
        />
      </div>
      <div className="provider-field">
        <label className="project-field-label" htmlFor="provider-url">Base URL</label>
        <input
          id="provider-url"
          className="project-input"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="https://api.example.com/v1"
        />
      </div>
      <div className="provider-field">
        <label className="project-field-label" htmlFor="provider-model">模型</label>
        <input
          id="provider-model"
          className="project-input"
          value={modelId}
          onChange={(e) => setModelId(e.target.value)}
          placeholder="模型名，如 deepseek-chat"
        />
      </div>
      <div className="provider-field">
        <label className="project-field-label" htmlFor="provider-key">API Key</label>
        <div className="provider-key-row">
          <input
            id="provider-key"
            className="project-input"
            type={showKey ? 'text' : 'password'}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={hasSavedKey ? '已保存（不回显）' : '输入 API Key'}
            autoComplete="off"
          />
          <button
            type="button"
            className="provider-key-toggle"
            aria-label={showKey ? '隐藏 API Key' : '显示 API Key'}
            onClick={() => setShowKey((v) => !v)}
          >
            {showKey ? <EyeOff size={13} /> : <Eye size={13} />}
          </button>
        </div>
        {hasSavedKey && (
          confirmingClearKey ? (
            <div className="provider-confirm-row">
              确认清除已保存的密钥？
              <button type="button" className="project-btn project-btn-danger" onClick={() => void clearKey()}>
                清除
              </button>
              <button type="button" className="project-btn" onClick={() => setConfirmingClearKey(false)}>
                取消
              </button>
            </div>
          ) : (
            <button type="button" className="provider-link-btn" onClick={() => setConfirmingClearKey(true)}>
              清除已保存的密钥
            </button>
          )
        )}
      </div>

      {testResult && (
        <div className={`provider-test-result ${testResult.ok ? 'ok' : 'fail'}`} role="status">
          {testResult.ok
            ? testResult.message
            : <span className="provider-test-msg">{testResult.message}</span>}
          {!testResult.ok && testResult.detail && (
            <details className="provider-test-detail">
              <summary>原始摘要</summary>
              <code>{testResult.detail}</code>
            </details>
          )}
        </div>
      )}
      {error && <div className="project-error" role="alert">{error}</div>}

      <div className="provider-form-actions">
        {isEdit ? (
          <>
            <button type="button" className="project-btn" onClick={() => void runTest()} disabled={testPending}>
              {testPending ? '测试中…' : '测试连接'}
            </button>
            <button type="button" className="project-btn project-btn-primary" onClick={() => void save()} disabled={pending}>
              {pending ? '保存中…' : '保存'}
            </button>
          </>
        ) : (
          <>
            {onDiscard && (
              <button type="button" className="project-btn" onClick={onDiscard}>取消</button>
            )}
            <button type="button" className="project-btn project-btn-primary" onClick={() => void save()} disabled={pending}>
              {pending ? '创建中…' : '创建'}
            </button>
          </>
        )}
      </div>

      {isEdit && (
        <div className="provider-danger-zone">
          {confirmingDelete ? (
            <div className="provider-confirm-row">
              <TriangleAlert size={12} aria-hidden="true" />
              确认删除供应商 {entry!.id}？引用它的会话下一轮会明确报错。
              <button type="button" className="project-btn project-btn-danger" onClick={() => void doDelete()}>
                删除
              </button>
              <button type="button" className="project-btn" onClick={() => setConfirmingDelete(false)}>
                取消
              </button>
            </div>
          ) : (
            <button type="button" className="provider-link-btn danger" onClick={() => setConfirmingDelete(true)}>
              <Trash2 size={12} aria-hidden="true" /> 删除供应商
            </button>
          )}
        </div>
      )}
    </div>
  );
}
