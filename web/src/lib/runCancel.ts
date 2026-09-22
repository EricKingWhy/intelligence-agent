/** 用户取消的 run 终态判据——**只此一处**。
 *
 *  `run/failed` 带 `reason === 'cancelled'` = 用户主动中断（Esc / 停止按钮），
 *  不是错误（da394a9）。读它的地方有三处，必须同源：
 *  - 会话投影 → `ConversationState.run_cancelled`（`projection.ts`）
 *  - 顶栏 / Inspector 概览的脉冲（`runState.ts` 读上面那个布尔）
 *  - Inspector 时间线的 run 分组头（`timelineGroups.ts`，按**组内事件**判，
 *    因为分组状态是 per-run 的，而 `run_cancelled` 只记最近一个 run）
 *
 *  三处各写一次 `reason === 'cancelled'` 就是真机审计 A-02 的病根：同一次取消，
 *  分组头徽章说「失败」、顶栏脉冲说「已取消」。规则变化时这里改一次即可。
 */
export function isCancelledRunFailure(data: unknown): boolean {
  return (data as { reason?: unknown } | null | undefined)?.reason === 'cancelled';
}
