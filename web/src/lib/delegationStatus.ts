/** 委派四态的**状态列 class 后缀单一事实源**（DSH 四态语言，completed→success 共享状态色）。
 *
 *  DelegationNode（Trace Ladder）、#1256 roster 行、分栏状态 pill 三处共用，避免各写一份
 *  映射（改色/改名即分叉，正是 #1256 AC2「四态点色与 DelegationNode 一致」要防的）。
 *  值域与 `Delegation['status']` / `DelegationEntry['status']`（均为同四态）一致。
 */
export type DelegationStatus = 'running' | 'completed' | 'failed' | 'stopped';

export const DELEGATION_STATUS_CLASS: Record<DelegationStatus, string> = {
  running: 'running',
  completed: 'success',
  failed: 'failed',
  stopped: 'stopped',
};
