/** lib/followLatest — 贴底跟随原语的纯函数核（#95，S8，规格 03 §17）。
 *
 * 「跟随 / 上滚脱离 / 跳到最新」三态的转移逻辑收在这里锁测试；组件层
 * （ReasoningBlock ExpandedBody、ToolCard ToolOutputStream）消费。推广为
 * 跨视图共享 useFollowLatest 原语的时机与形状收敛记 ADR-0016 议题
 * （T2 不引入抽象）。阈值是 tuning 参数。 */

import { useEffect } from 'react';

/** 贴底判定容差（px）。 */
export const FOLLOW_THRESHOLD_PX = 48;

/** 跟随状态：following = 新内容自动滚到底；suspended = 用户脱离，浮现「↓ 跳到最新」。 */
export interface FollowState {
  following: boolean;
  suspended: boolean;
}

export const FOLLOW_BOTTOM: FollowState = { following: true, suspended: false };

/** nearBottom 谓词：滚动容器是否贴底（严格小于容差）。 */
export function nearBottom(
  scrollHeight: number,
  scrollTop: number,
  clientHeight: number,
  threshold: number = FOLLOW_THRESHOLD_PX,
): boolean {
  return scrollHeight - scrollTop - clientHeight < threshold;
}

/** scroll 事件转移：贴底即恢复跟随（suspended 解除）；脱离且流式中 → suspended；
 *  脱离但非流式 → 只停跟随，不浮现「跳到最新」（没有「最新」可跳）。 */
export function followOnScroll(prev: FollowState, near: boolean, streaming: boolean): FollowState {
  if (near) return FOLLOW_BOTTOM;
  return streaming
    ? { following: false, suspended: true }
    : { following: false, suspended: prev.suspended };
}

/** 「↓ 跳到最新」动作转移：回底并恢复跟随。 */
export function followOnJump(): FollowState {
  return FOLLOW_BOTTOM;
}

/** 流结束复位跟随（终态无「最新」可跳——suspended 浮标不得残留）。
 *  T3 由 Standards 轴 Duplicated Code finding 收敛：ToolOutputStream 与
 *  ReasoningBlock ExpandedBody 的同族转移，单一实现。 */
export function useFollowResetOnStop(
  streaming: boolean,
  followRef: { current: FollowState },
  setSuspended: (v: boolean) => void,
): void {
  useEffect(() => {
    if (!streaming) {
      followRef.current = FOLLOW_BOTTOM;
      setSuspended(false);
    }
  }, [streaming, setSuspended]);
}
