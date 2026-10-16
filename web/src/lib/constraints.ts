/** 约束管理页（#1190 / #1176 Phase 1）的纯展示助手。
 *
 *  放在 `lib/` 而不是组件模块内：组件模块只导出组件（react-refresh 的
 *  `only-export-components`），这些纯函数直接单测、也可被别处复用。 */

/** ISO 时间 → `MM-DD HH:mm`（本地时区）；坏值原样返回，不猜。 */
export function formatFactTime(iso: string | null): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 操作人展示：本地信任身份的哨兵 `local` → 「本人操作」，其余原样（真实优先）。 */
export function operatorLabel(revokedBy: string | null): string {
  if (!revokedBy) return '';
  return revokedBy === 'local' ? '本人操作' : revokedBy;
}
