/** 记忆管理面的纯展示逻辑（MEM-5 / #160）。
 *
 *  与 `lib/projects.ts` 同一分工：组件只管渲染，取值/文案/解析规则放这里，
 *  可用纯函数测试锁住（不依赖 SSR/浏览器环境）。 */

import type { MemoryScope, MemorySummary } from '../types';
import type { MemoryKind, MemoryPayload, MemoryRecord, SemanticCategory } from './memoryV2Api';

export const MEMORY_CONTENT_MAX_CHARS = 500;
export const MEMORY_PAYLOAD_FIELDS: Record<MemoryKind, readonly { key: string; label: string }[]> = {
  semantic: [
    { key: 'subject', label: '主体' },
    { key: 'fact', label: '事实' },
  ],
  episodic: [
    { key: 'situation', label: '情境' },
    { key: 'action', label: '行动' },
    { key: 'outcome', label: '结果' },
    { key: 'lesson', label: '经验' },
  ],
  procedural: [
    { key: 'trigger', label: '触发条件' },
    { key: 'procedure', label: '操作步骤' },
    { key: 'success_condition', label: '成功条件' },
  ],
};
export const SEMANTIC_CATEGORIES: readonly { value: SemanticCategory; label: string }[] = [
  { value: 'preference', label: '偏好' },
  { value: 'profile', label: '个人画像' },
  { value: 'project_fact', label: '项目事实' },
  { value: 'constraint', label: '约束' },
];

export type MemoryPayloadDraft = Record<string, string>;

/** Match the backend's Python character-count and per-kind required-field contract. */
export function validateMemoryEdit(
  record: Pick<MemoryRecord, 'kind' | 'status'>,
  content: string,
  payload: MemoryPayload | null,
): string[] {
  const errors: string[] = [];
  if (record.kind === null || record.status !== 'active') errors.push('只有当前有效的 V2 记忆可以编辑。');
  if (!content.trim()) errors.push('记忆正文不能为空。');
  if (Array.from(content).length > MEMORY_CONTENT_MAX_CHARS) {
    errors.push(`记忆正文不能超过 ${MEMORY_CONTENT_MAX_CHARS} 个字符。`);
  }
  if (record.kind === null || payload === null || payload.kind !== record.kind) {
    errors.push('结构化字段必须与记忆类型匹配。');
    return errors;
  }
  const fields = MEMORY_PAYLOAD_FIELDS[record.kind];
  for (const { key, label } of fields) {
    const value = payload[key as keyof MemoryPayload];
    if (typeof value !== 'string' || !value.trim()) errors.push(`${label}不能为空。`);
    else if (Array.from(value).length > MEMORY_CONTENT_MAX_CHARS) {
      errors.push(`${label}不能超过 ${MEMORY_CONTENT_MAX_CHARS} 个字符。`);
    }
  }
  if (record.kind === 'semantic'
    && payload.kind === 'semantic'
    && !SEMANTIC_CATEGORIES.some(({ value }) => value === payload.category)) {
    errors.push('请选择有效的语义类别。');
  }
  return errors;
}

export function memoryPayloadFromDraft(
  kind: MemoryKind,
  values: MemoryPayloadDraft,
): MemoryPayload | null {
  if (kind === 'semantic') {
    const category = SEMANTIC_CATEGORIES.find(({ value }) => value === values.category)?.value;
    if (!category) return null;
    return { kind, subject: values.subject ?? '', fact: values.fact ?? '', category };
  }
  if (kind === 'episodic') {
    return {
      kind,
      situation: values.situation ?? '',
      action: values.action ?? '',
      outcome: values.outcome ?? '',
      lesson: values.lesson ?? '',
    };
  }
  return {
    kind,
    trigger: values.trigger ?? '',
    procedure: values.procedure ?? '',
    success_condition: values.success_condition ?? '',
  };
}

export function memoryPayloadDraft(payload: MemoryPayload): MemoryPayloadDraft {
  return Object.fromEntries(
    Object.entries(payload).map(([key, value]) => [key, String(value)]),
  );
}

/** 单页条数：与后端 `web/memory.py` 的默认值一致（`limit=50`，上界 200）。
 *
 *  "加载更多"每次追加这么多——刻意**不**一次拉全量（AC1 明确要求分页）：
 *  记忆正文可能很长（后端为此设了 200 的硬闸），一次拉全量在真实记忆库上
 *  既慢又白耗内存。 */
export const MEMORY_PAGE_SIZE = 50;

/** 后端单页**硬上界**（`web/memory.py::_MAX_LIMIT`：`Query(50, ge=1, le=200)`）。
 *
 *  前端必须知道这个数：重拉时如果照抄"用户已加载的条数"，加载超过 200 条后
 *  （4 次"加载更多"）请求就带 `limit=250` → 后端 **422** → 删除失败后的回滚重拉
 *  与"重试"按钮**永久失败**，列表再也回不到权威状态（AC3）。 */
export const MEMORY_MAX_LIMIT = 200;

/** 重拉（删除后对账 / 用户点重试）时的 `limit`：保留用户已加载的页数
 *  （**不能**一重拉就缩回第一页——那会凭空没收用户已经看到的内容），
 *  但不越过后端硬上界（越过就是 422，见 `MEMORY_MAX_LIMIT`）。
 *
 *  代价（明知且可接受）：已加载超过 200 条时，重拉只带回前 200 条，
 *  多出来的部分要靠用户再点一次"加载更多"——比"永久失败"好，且不撒谎。 */
export function refetchLimit(loaded: number): number {
  return Math.min(Math.max(loaded, MEMORY_PAGE_SIZE), MEMORY_MAX_LIMIT);
}

/** scope → 界面文案。`session` 也如实渲染：HTTP 用户入口今天只返回 USER 行
 *  （`web/memory.py` 的边界），但类型上保留两者，管理面不能把未知值当 user
 *  展示——那会把"这条记在某个会话名下"说成"记在你名下"。 */
export function scopeLabel(scope: MemoryScope): string {
  return scope === 'session' ? '会话' : '用户';
}

/** `created_at`（后端 ISO 字符串）→ 本地时间展示。
 *
 *  **解析失败不伪造时间**：原样返回后端给的字符串（比显示 "Invalid Date" 或
 *  补一个当前时间诚实——后者会让用户以为这条是刚记的）。空串同理原样返回。
 *  格式化只发生在这里，`types.ts` 的契约字段始终保持后端原值。 */
export function formatMemoryTime(createdAt: string): string {
  const at = new Date(createdAt);
  if (Number.isNaN(at.getTime())) return createdAt;
  return at.toLocaleString();
}

/** 列表是否还有下一页：请求了 `limit` 条、后端正好给满 `limit` 条才认为可能还有。
 *
 *  这是**启发式**而不是权威（后端不返回 total）：宁可多显示一次"加载更多"
 *  （点了返回空 → 下一次 hasMore 变 false），也不要在正好取满时把后续页藏掉。
 *  取少一条就确定到底了——后端按 offset 分页，返回少于请求量只可能是数据到底。 */
export function hasMoreAfter(received: number, requested: number): boolean {
  return requested > 0 && received >= requested;
}

/** 删除过程中被乐观移出的行——仅用于**把权威列表里对应的行暂时藏起来**，
 *  请求收口（成功后的重拉 / 失败后的回滚重拉）即失效。
 *
 *  为什么允许这个短暂的本地投影：AC3 要求"删除后列表立即反映（该条消失）"。
 *  它与不变量 #22 的关系是**有对账的**——两个结局都会重拉权威列表：
 *  - 成功：重拉后该行由**后端**的缺席消失（本地集合只是让它在往返期间先不可见）；
 *  - 失败：重拉后该行回来（这就是 AC3 说的"回滚 UI"）。
 *  所以列表的**静止态**永远是最近一次后端响应，不存在"只做本地隐藏"的可能。 */
export function withoutIds(rows: MemorySummary[], removed: ReadonlySet<string>): MemorySummary[] {
  if (removed.size === 0) return rows;
  return rows.filter((row) => !removed.has(row.id));
}
