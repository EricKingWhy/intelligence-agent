/** followLatest 纯函数契约（#95，S8，规格 03 §17 跟随原语的状态机核）。 */

import { describe, expect, it } from 'vitest';
import { FOLLOW_THRESHOLD_PX, followOnJump, followOnScroll, nearBottom } from './followLatest';

describe('nearBottom — 贴底谓词', () => {
  it('距底 < 阈值 → true；= 阈值 → false（严格小于）', () => {
    const h = 1000, ch = 400;
    expect(nearBottom(h, h - ch - FOLLOW_THRESHOLD_PX + 1, ch)).toBe(true);
    expect(nearBottom(h, h - ch - FOLLOW_THRESHOLD_PX, ch)).toBe(false);
  });

  it('精确贴底（差 0）→ true', () => {
    expect(nearBottom(1000, 600, 400)).toBe(true);
  });

  it('自定义阈值生效', () => {
    expect(nearBottom(1000, 500, 400, 101)).toBe(true);
    expect(nearBottom(1000, 500, 400, 99)).toBe(false);
  });
});

describe('followOnScroll / followOnJump — 跟随状态转移', () => {
  it('贴底 → 回到跟随底态（suspended 解除）', () => {
    expect(followOnScroll({ following: false, suspended: true }, true, true)).toEqual({
      following: true, suspended: false,
    });
  });

  it('脱离底部且流式中 → suspended（S8：上滚停跟随 + 浮现跳到最新）', () => {
    expect(followOnScroll({ following: true, suspended: false }, false, true)).toEqual({
      following: false, suspended: true,
    });
  });

  it('脱离底部但非流式 → 不进入 suspended（无「最新」可跳）', () => {
    expect(followOnScroll({ following: true, suspended: false }, false, false)).toEqual({
      following: false, suspended: false,
    });
  });

  it('jump → 底态（S8：回底并恢复跟随）', () => {
    expect(followOnJump()).toEqual({ following: true, suspended: false });
  });
});
