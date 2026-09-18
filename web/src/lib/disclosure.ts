/** lib/disclosure — Progressive Disclosure L0-L2 状态机（ADR-0014 D2，PRD §6）。
 *
 * 双层模型：全局 density 决定每事件的**默认**展开级，用户手动展开进入
 * override map（手动优先于全局，切 density 不丢——PRD §6 规则）。
 * L3 不在本状态机内：L3 = Inspect 进 Inspector（联动，App 层 selected 状态）。
 *
 * 契约用纯函数锁（useSession.test 同款纪律：React 壳只做 useState 胶水）。
 * override 随 session 切换清空（hook 壳 effect），不跨会话记忆。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { TraceDensity } from './density';
import type { ReasoningStatus } from './reasoningCursor';

/** 中间主区展开级：L0 摘要行 / L1 inline detail / L2 advanced inline。
 *  L3（Inspector Raw）不是 inline 级，走联动选中。 */
export type DisclosureLevel = 0 | 1 | 2;

/** 全局 density → 默认展开级（PRD §7 四档语义）。 */
export function defaultLevelFor(density: TraceDensity): DisclosureLevel {
  switch (density) {
    case 'compact':
      return 0;
    case 'balanced':
      return 0;
    case 'detailed':
      return 1;
    case 'raw':
      return 2;
  }
}

/** 稳定事件 key：工具用 tool_call_id（跨重渲染稳定）；模型段用 step+index。
 *  命名空间前缀隔离两类 id 空间。 */
export function toolEventKey(toolCallId: string): string {
  return `tool:${toolCallId}`;
}

export function modelEventKey(stepId: number, segmentIndex: number): string {
  return `model:${stepId}:${segmentIndex}`;
}

/** 不可变 override map：set 后返回新 map（React 状态约定）。 */
export function applyOverride(
  overrides: ReadonlyMap<string, DisclosureLevel>,
  key: string,
  level: DisclosureLevel,
): ReadonlyMap<string, DisclosureLevel> {
  const next = new Map(overrides);
  next.set(key, level);
  return next;
}

/** 生效级 = 手动 override 优先，否则全局 density 默认（PRD §6："手动展开
 *  状态优先于全局模式"，切换四档不丢手动展开）。 */
export function resolveLevel(
  overrides: ReadonlyMap<string, DisclosureLevel>,
  key: string,
  density: TraceDensity,
): DisclosureLevel {
  return overrides.get(key) ?? defaultLevelFor(density);
}

/** 点击循环下一级：L0→L1→L2→L0（PRD §6"点击事件展开 / 再次展开 View details"）。 */
export function nextLevel(level: DisclosureLevel): DisclosureLevel {
  return ((level + 1) % 3) as DisclosureLevel;
}

/** Disclosure 消费面（Conversation/ToolCard props 类型）。 */
export interface Disclosure {
  /** 事件的生效级：override 优先，否则全局 density 默认。 */
  levelFor: (key: string, density: TraceDensity) => DisclosureLevel;
  /** 设置手动 override（点击事件行循环 L0→L1→L2→L0 时调用）。 */
  setLevel: (key: string, level: DisclosureLevel) => void;
}

/* ── F1（#270）：返回值跨渲染引用稳定 ─────────────────────────────────────────────
 *
 * 为什么必须稳定：返回对象会被 `Conversation.tsx` 一路当 prop 透传（`:355` → `:629`
 * → 链路渲染器 `:711`），同时 `reasoningDisclosure` 进 `ReasoningBlock`。不稳定 ⇒
 * `memo(TurnView)` / `memo(ToolCard)` / `memo(ReasoningBlockView)` 三处浅比较恒不等，
 * memo **恒 miss**。后果是流式期间约 40 次/秒的合帧提交里，屏幕上每个已完成的可见
 * model 段都被重跑一次全量 markdown 解析（机制与前后数字见 docs/PERF_BASELINE.md
 * 的 F1 节）——正是「长回答越写越卡」。
 *
 * 为什么选票面必做 1 的 **B 方案**（整体 useMemo）而不是标注「推荐」的 A 方案
 * （useRef 稳定容器，身份**永不改变**）：A 会引入一条可复现的**功能缺陷**，不是风格问题。
 * `levelFor` 是在 `TurnView` **自己的渲染体**里被调用、用来算每个工具卡的 `level` 的
 * （Conversation.tsx:711）。点了档位 → `setLevel` → overrides 变 → **必须**让
 * `memo(TurnView)` 重新比较出「不等」，TurnView 才会重渲染、新的 level 才流得到
 * ToolCard。身份永不改变的 A 方案下 memo 恒 bail out ⇒ 点击工具行的档位循环静默无效
 * （实测红证：Conversation.render.test.tsx 的「稳定化不得变成『点了没反应』」用例
 * 在 A 方案下断言 `expected 1 to be greater than 1`）。
 *
 * B 的代价是「依赖集合必须补全」，漏一个就是陈旧读取（票面 Risks 点名的陷阱）。两个
 * hook 的捕获面都在下面逐项注明；`levelFor`/`isOpen` 的 `density` 是**调用方传入的
 * 参数**（`levelFor`）或显式依赖（`isOpen`），都不落在「捕获了却漏进依赖」的坑里。
 */

/** 逐会话的手动展开状态（hook 壳：sessionKey 变化即清空）。 */
export function useDisclosure(sessionKey: string | null): Disclosure {
  const [overrides, setOverrides] = useState<ReadonlyMap<string, DisclosureLevel>>(() => new Map());

  /* 清空 override 只应发生在 **sessionKey 真的变了** 的时候。挂载那一次必须跳过：
   * state 初值本就是一张空 Map，`setOverrides(new Map())` 只是把引用换成内容相同的新表
   * ——白渲染一次，并按下面 useMemo 的依赖捅出一个「无内容变化的新引用」，R1 的
   * 字面口径（同一实例连续两次渲染 `===` 相等）会因此破。这不是放宽断言，是消掉一次
   * 可证明无内容变化的状态写入。 */
  const lastSessionKey = useRef(sessionKey);
  useEffect(() => {
    if (lastSessionKey.current === sessionKey) return;
    lastSessionKey.current = sessionKey;
    setOverrides(new Map());
  }, [sessionKey]);

  const setLevel = useCallback((key: string, level: DisclosureLevel) => {
    setOverrides((prev) => applyOverride(prev, key, level));
  }, []);

  /* 捕获面核对：`levelFor` 只捕获 `overrides`（density 是调用方的参数，不进闭包）；
   * `setLevel` 只捕获 useCallback 出来的 `setLevel`（空依赖，本身稳定）
   * ⇒ 依赖集合 `[overrides, setLevel]` 完整。 */
  return useMemo<Disclosure>(() => ({
    levelFor: (key, density) => resolveLevel(overrides, key, density),
    setLevel,
  }), [overrides, setLevel]);
}

// ── T2（#95）reasoning 自动开合（S6/S7，规格 03 §7.5 DisclosureState）──

/** 自动规则：streaming 且非 compact → 开（Balanced/Detailed 默认自动展开）；
 *  其余 → 收（完成自动收、interrupted 收、compact 一行实况）。 */
export function reasoningIsOpen(
  overrides: ReadonlyMap<string, boolean>,
  blockId: string,
  status: ReasoningStatus,
  density: TraceDensity,
): boolean {
  const manual = overrides.get(blockId);
  if (manual !== undefined) return manual;
  return status === 'streaming' && density !== 'compact';
}

/** 手动开合：写入 override 即 user_interacted=true——此后 delta/完成/密度切换
 *  都不改写该块（S7：手动折叠期间流继续，绝不重开）。 */
export function setReasoningOpen(
  overrides: ReadonlyMap<string, boolean>,
  blockId: string,
  open: boolean,
): ReadonlyMap<string, boolean> {
  const next = new Map(overrides);
  next.set(blockId, open);
  return next;
}

/** 逐会话的 reasoning 开合状态（override 随 sessionKey 变化清空——
 *  呈现状态是页内局部的，不进持久真相，规格 01 §17）。 */
export function useReasoningDisclosure(sessionKey: string | null, density: TraceDensity) {
  const [overrides, setOverrides] = useState<ReadonlyMap<string, boolean>>(() => new Map());

  // sessionKey 变化清空；挂载期跳过（理由同 useDisclosure）。
  const lastSessionKey = useRef(sessionKey);
  useEffect(() => {
    if (lastSessionKey.current === sessionKey) return;
    lastSessionKey.current = sessionKey;
    setOverrides(new Map());
  }, [sessionKey]);

  const toggle = useCallback((blockId: string, currentOpen: boolean) => {
    setOverrides((prev) => setReasoningOpen(prev, blockId, !currentOpen));
  }, []);

  /* F1（#270）：与 useDisclosure 同一条引用稳定契约（`memo(ReasoningBlockView)` 直接吃
   * 这个对象）。捕获面核对：`isOpen` 捕获 `overrides` + `density`（**prop**，必须进依赖：
   * 切 density 后自动开合规则要按新档重新求值，且结果会变，memo 必须重算）；
   * `toggle` 捕获 useCallback 的 `toggle`（空依赖）⇒ `[overrides, density, toggle]` 完整。 */
  return useMemo(() => ({
    isOpen: (blockId: string, status: ReasoningStatus) =>
      reasoningIsOpen(overrides, blockId, status, density),
    toggle: (blockId: string, currentOpen: boolean) => toggle(blockId, currentOpen),
  }), [overrides, density, toggle]);
}
