/**
 * 跨端共享的**纯事件语义**（#1498，裁决 10 的 D7-B）。
 *
 * 这里只放**可证明两端同形**的判断规则，收口两端各写一份的重复：
 *   1. `RUN_TERMINAL_TYPES`：哪些事件算 run 终态（「这个 run 收口了吗」是**一个**判断）；
 *   2. `planItemsFromData`：`task/plan_updated` 的整表覆盖 + 行级容错解析（实测归一化同构）；
 *   3. `supersedeRanges`：`message/superseded` 的区间规则（ADR-0030 第 4.2、5.4 节）。
 *
 * 不放在这里的（卷宗明写，范围边界）：Web 的 COW、segments、DOM，TUI 的组件，
 * 以及两端各自的 Turn 模型。两端只消费本模块的**结果**，不共享状态容器。
 *
 * 为什么文件在 `tui/src/shared/` 而不是仓根 `shared/` 或 `web/src/`：TUI 的
 * tsconfig 是 `rootDir: "."`，`tui/` 之外的任何文件都会撞 TS6059（消融实测见
 * #1498 回报），所以落点只能是 tui 包内。位置由构建边界决定，不代表归属：
 * Web 以相对路径直接消费本文件，两端因此**只有一份实现**，没有第二副本要守。
 */
import { EventType } from "../generated/event-types.ts";

/** run 终态词汇，镜像后端 `event.py:RUN_TERMINAL_TYPES`。
 *
 *  `run/paused` **不在**集合里：暂停是非终态（#312，后端 03 第 5 节），把它当「跑完了」
 * 会让终态判定、时长结算、恢复入口全走错分支。它对**执行区间**另有收口语义，
 * 由各端各自的 run 扫描处理（Web `scanRuns`、TUI 的终态清扫），不走本集合。
 */
export const RUN_TERMINAL_TYPES: ReadonlySet<string> = new Set<string>([
  EventType.RUN_COMPLETED,
  EventType.RUN_FAILED,
  EventType.RUN_INTERRUPTED,
]);

/** 进度清单行（#381 W-27、#382 W-28；PRD 长任务 7.1 节五字段）。
 *
 *  `status` 刻意放宽为 string：服务端 handler 已硬校验三态，投影不改写未知值
 *  （不改写、不猜测状态机），未知状态的渲染容错归各端视图。
 */
export interface PlanItem {
  id: string;
  content: string;
  activeForm: string;
  status: string;
  source: string;
}

/** `task/plan_updated.data` 转为清单行数组；`data.items` 非数组返回 null（坏帧整个忽略）。
 *
 *  容错哲学与后端 `derive_plan` 同一纪律：一行坏数据只损失该行，不 brick 恢复链。
 *  空数组是合法清空（返回空数组），与非数组坏帧（null）必须区分。
 */
export function planItemsFromData(data: Record<string, unknown>): PlanItem[] | null {
  const rawItems = data["items"];
  if (!Array.isArray(rawItems)) return null;
  const items: PlanItem[] = [];
  const seen = new Set<string>();
  for (const raw of rawItems) {
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) continue;
    const row = raw as Record<string, unknown>;
    const id = row["id"];
    if (typeof id !== "string" || id === "" || seen.has(id)) continue;
    seen.add(id);
    const content = typeof row["content"] === "string" ? row["content"] : "";
    const activeForm = row["activeForm"];
    items.push({
      id,
      content,
      activeForm:
        typeof activeForm === "string" && activeForm !== "" ? activeForm : content,
      status: typeof row["status"] === "string" ? row["status"] : "",
      source: typeof row["source"] === "string" ? row["source"] : "",
    });
  }
  return items;
}

/**
 * supersede 区间（ADR-0030 第 4.2、5.4 节，与后端 `derive.py` 同构）。
 *
 * 被取代的 seq 为 s，区间 = [s, n)，n = s 之后第一条**未被取代**的 user 消息 seq；
 * 没有就一直到底（lastSeq + 1）。判据只看 seq 与「该 seq 是否也被取代」，
 * 与事件到达顺序无关（纯函数性质）。区间内的整轮从**视图**消失，事件日志照旧保留
 * （不变量 #3 append-only）。
 *
 * 入参是两端都能廉价取到的三样事实：被取代的 seq 集、user 消息的 seq 序列、
 * 已见的最大 seq。两端取法不同（Web 读 events 日志，TUI 读 turns），规则只有这一份。
 */
export function supersedeRanges(
  supersededSeqs: Iterable<number>,
  userSeqs: readonly number[],
  lastSeq: number,
): Array<[number, number]> {
  const superseded = new Set<number>();
  for (const seq of supersededSeqs) {
    // 一行坏数据只损失该行：非有限数忽略。
    if (typeof seq === "number" && Number.isFinite(seq)) superseded.add(seq);
  }
  if (superseded.size === 0) return [];
  const ranges: Array<[number, number]> = [];
  for (const seq of [...superseded].sort((a, b) => a - b)) {
    const following = userSeqs.find((u) => u > seq && !superseded.has(u));
    const end = following !== undefined ? following : lastSeq + 1;
    if (end > seq) ranges.push([seq, end]);
  }
  return ranges;
}

/** seq 是否落在任一区间内（渲染端据此让整轮退场）。 */
export function isShadowed(
  seq: number | null,
  ranges: readonly (readonly [number, number])[],
): boolean {
  if (seq === null) return false;
  return ranges.some(([start, end]) => start <= seq && seq < end);
}
