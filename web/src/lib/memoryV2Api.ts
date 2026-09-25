import { emitUnauthorized, getToken } from './auth';
import { MemoryError, UnauthorizedError, readErrorDetail } from './api';

export type MemoryKind = 'semantic' | 'episodic' | 'procedural';
export type MemoryStatus = 'active' | 'superseded' | 'invalidated' | 'deleted';
export type MemoryScopeV2 = 'user_global' | 'project';
export type MemoryTier = 'profile' | 'collection';
export type SemanticCategory = 'preference' | 'profile' | 'project_fact' | 'constraint';

export type MemoryPayload =
  | { kind: 'semantic'; subject: string; fact: string; category: SemanticCategory }
  | { kind: 'episodic'; situation: string; action: string; outcome: string; lesson: string }
  | { kind: 'procedural'; trigger: string; procedure: string; success_condition: string };

export interface MemoryRecord {
  id: string;
  content: string;
  scope: 'user' | 'session' | MemoryScopeV2;
  metadata: Record<string, unknown>;
  created_at: string;
  root_id: string | null;
  version: number | null;
  kind: MemoryKind | null;
  tier: MemoryTier | null;
  status: MemoryStatus | null;
  project_id: string | null;
  source_type: 'automatic' | 'explicit_command' | 'user_edit' | null;
  source_session_id: string | null;
  source_event_ids: string[];
  payload: MemoryPayload | null;
}

export interface MemoryFilters {
  q?: string;
  kind?: MemoryKind;
  status?: MemoryStatus;
  scope?: MemoryScopeV2;
  project_id?: string;
}

export interface MemorySettings {
  extraction_enabled: boolean;
  recall_enabled: boolean;
}

export interface MemoryRecallExplanation {
  memory_id: string;
  kind: MemoryKind;
  scope: MemoryScopeV2;
  source_type: 'automatic' | 'explicit_command' | 'user_edit';
  version: number;
  source_session_id: string | null;
  source_event_ids: string[];
  ranking: Record<string, string | number>;
}

export interface SessionMemoryRecall {
  run_id: string | null;
  seq: number;
  time: string;
  memories: MemoryRecallExplanation[];
}

export interface MemoryEditRequest {
  expected_version: number;
  content: string;
  payload: MemoryPayload;
}

const PAGE_LIMIT = 200;
const MAX_LIST_OFFSET = 10_000;
const MEMORY_KINDS: readonly MemoryKind[] = ['semantic', 'episodic', 'procedural'];
const MEMORY_STATUSES: readonly MemoryStatus[] = ['active', 'superseded', 'invalidated', 'deleted'];
const MEMORY_TIERS: readonly MemoryTier[] = ['profile', 'collection'];
const MEMORY_SCOPES: readonly MemoryScopeV2[] = ['user_global', 'project'];
const SOURCE_TYPES = ['automatic', 'explicit_command', 'user_edit'] as const;
const RANKING_FIELDS = new Set([
  'ranking_version', 'dense', 'keyword', 'importance', 'strength', 'source_authority',
  'kind_weight', 'scope_weight', 'age_days', 'decay', 'score',
]);

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isOneOf<T extends string>(value: unknown, values: readonly T[]): value is T {
  return typeof value === 'string' && values.includes(value as T);
}

function memoryRecordPath(memoryId: string, suffix = '', projectId?: string): string {
  const path = `/api/memories/${encodeURIComponent(memoryId)}${suffix}`;
  if (!projectId) return path;
  return `${path}?${new URLSearchParams({ project_id: projectId })}`;
}

function parsePayload(raw: unknown, kind: MemoryKind, allowDeleted: boolean): MemoryPayload | null {
  if (raw === null && allowDeleted) return null;
  if (!isObject(raw) || raw.kind !== kind) return null;
  const nonEmpty = (key: string) => typeof raw[key] === 'string' && raw[key].trim().length > 0;
  if (kind === 'semantic') {
    if (!nonEmpty('subject') || !nonEmpty('fact') || !isOneOf(raw.category, [
      'preference', 'profile', 'project_fact', 'constraint',
    ])) return null;
    return {
      kind,
      subject: raw.subject as string,
      fact: raw.fact as string,
      category: raw.category,
    };
  }
  if (kind === 'episodic') {
    if (!nonEmpty('situation') || !nonEmpty('action') || !nonEmpty('outcome') || !nonEmpty('lesson')) {
      return null;
    }
    return {
      kind,
      situation: raw.situation as string,
      action: raw.action as string,
      outcome: raw.outcome as string,
      lesson: raw.lesson as string,
    };
  }
  if (!nonEmpty('trigger') || !nonEmpty('procedure') || !nonEmpty('success_condition')) return null;
  return {
    kind,
    trigger: raw.trigger as string,
    procedure: raw.procedure as string,
    success_condition: raw.success_condition as string,
  };
}

function parseMemoryRecord(raw: unknown): MemoryRecord | null {
  if (!isObject(raw)) return null;
  if (typeof raw.id !== 'string' || !raw.id || typeof raw.content !== 'string') return null;
  if (typeof raw.created_at !== 'string' || !raw.created_at) return null;
  if (!['user', 'session', ...MEMORY_SCOPES].includes(raw.scope as string)) return null;
  const scope = raw.scope as MemoryRecord['scope'];
  const metadata = isObject(raw.metadata) ? raw.metadata : {};
  const base = {
    id: raw.id,
    content: raw.content,
    scope,
    metadata,
    created_at: raw.created_at,
  };
  const hasV2Shape = MEMORY_SCOPES.includes(scope as MemoryScopeV2)
    || typeof raw.kind === 'string'
    || typeof raw.root_id === 'string'
    || typeof raw.version === 'number';
  if (!hasV2Shape) {
    return {
      ...base,
      root_id: null,
      version: null,
      kind: null,
      tier: null,
      status: null,
      project_id: null,
      source_type: null,
      source_session_id: null,
      source_event_ids: [],
      payload: null,
    };
  }
  if (!isOneOf(raw.kind, MEMORY_KINDS)
    || !isOneOf(raw.tier, MEMORY_TIERS)
    || !isOneOf(raw.status, MEMORY_STATUSES)
    || !isOneOf(raw.source_type, SOURCE_TYPES)
    || typeof raw.root_id !== 'string' || !raw.root_id
    || typeof raw.version !== 'number' || !Number.isSafeInteger(raw.version) || raw.version < 1
    || (raw.project_id !== null && typeof raw.project_id !== 'string')
    || (raw.source_session_id !== null && typeof raw.source_session_id !== 'string')
    || !Array.isArray(raw.source_event_ids)
    || !raw.source_event_ids.every((id) => typeof id === 'string')) return null;
  const payload = parsePayload(raw.payload, raw.kind, raw.status === 'deleted');
  if (raw.status !== 'deleted' && payload === null) return null;
  return {
    ...base,
    root_id: raw.root_id,
    version: raw.version,
    kind: raw.kind,
    tier: raw.tier,
    status: raw.status,
    project_id: raw.project_id,
    source_type: raw.source_type,
    source_session_id: raw.source_session_id,
    source_event_ids: raw.source_event_ids,
    payload,
  };
}

async function memoryRequest(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(path, { ...init, headers });
  if (response.status === 401) {
    const detail = await readErrorDetail(response);
    emitUnauthorized(detail || 'Missing identity token');
    throw new UnauthorizedError(detail || '需要身份令牌（401）');
  }
  return response;
}

async function memoryError(response: Response, fallback: string): Promise<MemoryError> {
  const body: unknown = await response.json().catch(() => null);
  const detail = isObject(body) ? body.detail : null;
  const message = typeof detail === 'string'
    ? detail
    : isObject(detail) && typeof detail.message === 'string'
      ? detail.message
      : '';
  const code = isObject(detail) && typeof detail.code === 'string' ? detail.code : null;
  return new MemoryError(response.status, message || `${fallback}（${response.status}）`, code);
}

async function requireOk(response: Response, fallback: string): Promise<void> {
  if (!response.ok) throw await memoryError(response, fallback);
}

async function requireRecord(response: Response, fallback: string): Promise<MemoryRecord> {
  await requireOk(response, fallback);
  const record = parseMemoryRecord(await response.json());
  if (!record) throw new Error(`${fallback}（后端响应形状不符）`);
  return record;
}

export async function listMemoryRecords(
  limit = 50,
  offset = 0,
  filters: MemoryFilters = {},
): Promise<MemoryRecord[]> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  const query = filters.q?.trim();
  if (query) params.set('q', query);
  if (filters.kind) params.set('kind', filters.kind);
  if (filters.status) params.set('status', filters.status);
  if (filters.scope) params.set('scope', filters.scope);
  if (filters.project_id) params.set('project_id', filters.project_id);
  const response = await memoryRequest(`/api/memories?${params}`);
  await requireOk(response, '加载记忆失败');
  const body: unknown = await response.json();
  return Array.isArray(body)
    ? body.flatMap((raw) => {
      const record = parseMemoryRecord(raw);
      return record ? [record] : [];
    })
    : [];
}

export async function getMemoryRecord(memoryId: string, projectId?: string): Promise<MemoryRecord> {
  return requireRecord(
    await memoryRequest(memoryRecordPath(memoryId, '', projectId)),
    '读取记忆失败',
  );
}

export async function getMemoryVersions(memoryId: string, projectId?: string): Promise<MemoryRecord[]> {
  const response = await memoryRequest(
    memoryRecordPath(memoryId, '/versions', projectId),
  );
  await requireOk(response, '加载记忆版本失败');
  const body: unknown = await response.json();
  if (!Array.isArray(body)) throw new Error('记忆版本响应形状不符');
  return body.flatMap((raw) => {
    const record = parseMemoryRecord(raw);
    return record ? [record] : [];
  });
}

export async function editMemoryRecord(
  memoryId: string,
  request: MemoryEditRequest,
  projectId?: string,
): Promise<MemoryRecord> {
  return requireRecord(
    await memoryRequest(memoryRecordPath(memoryId, '', projectId), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    }),
    '保存记忆失败',
  );
}

export async function deleteMemoryRecord(memoryId: string, projectId?: string): Promise<boolean> {
  const response = await memoryRequest(memoryRecordPath(memoryId, '', projectId), {
    method: 'DELETE',
  });
  await requireOk(response, '删除记忆失败');
  const body: unknown = await response.json();
  return isObject(body) && body.id === memoryId && body.deleted === true;
}

export async function bulkDeleteMemoryRecords(
  kind: MemoryKind | null,
  projectId?: string,
): Promise<{ affected_count: number; deleted_version_count: number }> {
  const path = projectId
    ? `/api/memories/bulk-delete?${new URLSearchParams({ project_id: projectId })}`
    : '/api/memories/bulk-delete';
  const response = await memoryRequest(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, confirmation: 'DELETE' }),
  });
  await requireOk(response, '批量删除记忆失败');
  const body: unknown = await response.json();
  if (!isObject(body)
    || !Number.isSafeInteger(body.affected_count) || (body.affected_count as number) < 0
    || !Number.isSafeInteger(body.deleted_version_count) || (body.deleted_version_count as number) < 0) {
    throw new Error('批量删除响应形状不符');
  }
  return {
    affected_count: body.affected_count as number,
    deleted_version_count: body.deleted_version_count as number,
  };
}

export async function countMemoryRoots(
  kind?: MemoryKind,
  projectId?: string,
): Promise<{ count: number; exact: boolean }> {
  const roots = new Set<string>();
  const contexts = projectId ? [undefined, projectId] : [undefined];
  for (const context of contexts) {
    for (let offset = 0; ; offset += PAGE_LIMIT) {
      const page = await listMemoryRecords(PAGE_LIMIT, offset, {
        ...(kind ? { kind } : {}),
        ...(context ? { project_id: context } : {}),
      });
      for (const record of page) {
        if (record.kind !== null && record.root_id !== null) roots.add(record.root_id);
      }
      if (page.length < PAGE_LIMIT) break;
      if (offset >= MAX_LIST_OFFSET) return { count: roots.size, exact: false };
    }
  }
  return { count: roots.size, exact: true };
}

function parseSettings(raw: unknown): MemorySettings | null {
  if (!isObject(raw)
    || typeof raw.extraction_enabled !== 'boolean'
    || typeof raw.recall_enabled !== 'boolean') return null;
  return {
    extraction_enabled: raw.extraction_enabled,
    recall_enabled: raw.recall_enabled,
  };
}

export async function getMemorySettings(): Promise<MemorySettings> {
  const response = await memoryRequest('/api/memory-settings');
  await requireOk(response, '加载记忆设置失败');
  const settings = parseSettings(await response.json());
  if (!settings) throw new Error('记忆设置响应形状不符');
  return settings;
}

export async function updateMemorySettings(
  patch: Partial<MemorySettings>,
): Promise<MemorySettings> {
  const response = await memoryRequest('/api/memory-settings', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  await requireOk(response, '保存记忆设置失败');
  const settings = parseSettings(await response.json());
  if (!settings) throw new Error('记忆设置响应形状不符');
  return settings;
}

function parseRecallExplanation(raw: unknown): MemoryRecallExplanation | null {
  if (!isObject(raw)
    || typeof raw.memory_id !== 'string'
    || !isOneOf(raw.kind, MEMORY_KINDS)
    || !isOneOf(raw.scope, MEMORY_SCOPES)
    || !isOneOf(raw.source_type, SOURCE_TYPES)
    || typeof raw.version !== 'number'
    || !Number.isSafeInteger(raw.version)
    || raw.version < 1
    || (raw.source_session_id !== null && typeof raw.source_session_id !== 'string')
    || !Array.isArray(raw.source_event_ids)
    || !raw.source_event_ids.every((id) => typeof id === 'string')) return null;
  const ranking: Record<string, string | number> = {};
  if (isObject(raw.ranking)) {
    for (const [key, value] of Object.entries(raw.ranking)) {
      if (RANKING_FIELDS.has(key)
        && (typeof value === 'string' || (typeof value === 'number' && Number.isFinite(value)))) {
        ranking[key] = value;
      }
    }
  }
  return {
    memory_id: raw.memory_id,
    kind: raw.kind,
    scope: raw.scope,
    source_type: raw.source_type,
    version: raw.version,
    source_session_id: raw.source_session_id,
    source_event_ids: raw.source_event_ids,
    ranking,
  };
}

export async function getSessionMemoryRecalls(sessionId: string): Promise<SessionMemoryRecall[]> {
  const response = await memoryRequest(
    `/api/sessions/${encodeURIComponent(sessionId)}/memory-recalls`,
  );
  await requireOk(response, '加载记忆召回说明失败');
  const body: unknown = await response.json();
  if (!Array.isArray(body)) throw new Error('记忆召回说明响应形状不符');
  return body.flatMap((raw) => {
    if (!isObject(raw)
      || (raw.run_id !== null && typeof raw.run_id !== 'string')
      || typeof raw.seq !== 'number' || !Number.isSafeInteger(raw.seq)
      || typeof raw.time !== 'string' || !Array.isArray(raw.memories)) return [];
    const memories = raw.memories.flatMap((item) => {
      const explanation = parseRecallExplanation(item);
      return explanation ? [explanation] : [];
    });
    return [{ run_id: raw.run_id, seq: raw.seq, time: raw.time, memories }];
  });
}
