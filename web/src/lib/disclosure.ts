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
 * ⚠ **操作约束（改下面两个 hook 前必读）**：返回对象被 `Conversation.tsx` 一路当 prop 透传
 * 到 `memo(TurnView)` / `memo(ToolCard)` / `memo(ReasoningBlockView)`。每次渲染返回新对象 ⇒
 * 三处 memo **恒 miss** ⇒ 流式期间每个可见 model 段被反复重跑全量 markdown 解析（「长回答
 * 越写越卡」）。**加字段就要同步补依赖**，漏一个就是陈旧读取。
 *
 * ⚠ 但**不能**因此把返回对象做成"身份永不改变"（票面的 A 方案）：`levelFor` 是在 `TurnView`
 * 自己的渲染体里被调用来算工具卡 `level` 的，memo 恒 bail out ⇒ 点击档位循环静默无效。
 * 为什么取 B 方案（整体 `useMemo` + 依赖补全）、否决 A，以及两个 hook 的捕获面逐项核对，
 * 见 `docs/adr/0037-projection-reference-stability-and-events-version.md` D5.2。
 */

/** 逐会话的手动展开状态（hook 壳：sessionKey 变化即清空）。 */
export function useDisclosure(sessionKey: string | null): Disclosure {
  const [overrides, setOverrides] = useState<ReadonlyMap<string, DisclosureLevel>>(() => new Map());

  /* 清空 override 只应发生在 **sessionKey 真的变了** 的时候。⚠ 挂载那一次必须跳过：
   * 否则会写进一张内容相同的新空 Map——白渲染一次，并捅出一个「无内容变化的新引用」。
   * 为什么这不是"放宽断言"，见 ADR-0037 D5.2。 */
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

  /* F1（#270）：与 `useDisclosure` 同一条契约（ADR-0037 D5.2）。捕获面核对：`isOpen`
   * 捕获 `overrides` + `density`（**prop，必须进依赖**：切 density 后自动开合规则要按新档
   * 重新求值且结果会变，memo 必须重算）；`toggle` 捕获空依赖 useCallback
   * ⇒ `[overrides, density, toggle]` 完整。 */
  return useMemo(() => ({
    isOpen: (blockId: string, status: ReasoningStatus) =>
      reasoningIsOpen(overrides, blockId, status, density),
    toggle: (blockId: string, currentOpen: boolean) => toggle(blockId, currentOpen),
  }), [overrides, density, toggle]);
}
