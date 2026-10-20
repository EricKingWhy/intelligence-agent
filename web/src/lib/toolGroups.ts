/** lib/toolGroups — 连续同类工具调用的**渲染层**分组投影（#1429 打磨点 1）。
 *
 * 纯函数、view over chain：输入是 `deriveChain` 的输出，输出是"单节点 / 分组"
 * 的渲染序列。不改事件模型、不改持久化、不改 `Turn` 形状——分组只是把连续同类
 * 的 tool 节点在**渲染时**收拢成一行组头，事件仍是唯一真相（不变量 #22）。
 *
 * 规则（票面写死，逐条有测试）：
 *  - 类别键复用 `eventKind.toolKind()`：read/grep/glob→search、edit/write/apply_patch→
 *    write、bash→terminal、mcp__*→mcp。`pytest` 走 bash 通道，随 terminal 归组。
 *  - 只有这四类成组：`tool`（通用扳手）/`subagent` 没有票面定义的组头文案，不猜，
 *    直接单节点渲染（它们同时会打断左右两侧的组）。
 *  - 断组：非 tool 节点（model / reasoning / delegation）、kind 变化、链结束。
 *  - 组内成员 ≥2 才成组；单个调用保持单节点。
 *  - 失败 / 运行中 / stopped 成员照常入组。
 *  - 组头状态聚合优先级：failed > running > stopped > success（票面点名前三档；
 *    stopped 是 DSH 四态语言里"中断 ≠ 错误"的第四态，不塌缩成"完成"）。
 *  - 组头耗时 = span：首成员 `started_at` → 末成员 `completed_at`；末成员仍在跑时
 *    `completedAt` 缺省，由渲染层按"当前时刻"续算（本层不读时钟，保持纯函数）。
 */

import type { ChainNode } from './projection';
import type { ToolCall } from '../types';
import { toolKind } from './eventKind';

/** 可成组的四类（组头文案见 GROUP_LABEL）。 */
export type GroupKind = 'search' | 'write' | 'terminal' | 'mcp';

/** 组头人话（票面指定：search=读取 / write=编辑 / terminal=命令 / mcp=MCP 调用）。 */
export const GROUP_LABEL: Record<GroupKind, string> = {
  search: '读取',
  write: '编辑',
  terminal: '命令',
  mcp: 'MCP 调用',
};

/** 静态成员表（四类可成组；其余 kind 不分组，见文件头）。 */
const IS_GROUP_KIND: Record<string, true> = {
  search: true,
  write: true,
  terminal: true,
  mcp: true,
};

/** 工具调用 → 可成组的类别键；不属于四类时 null（不分组、且打断相邻组）。 */
export function groupKindOf(tool: ToolCall): GroupKind | null {
  const kind = toolKind(tool.name);
  return IS_GROUP_KIND[kind] === true ? (kind as GroupKind) : null;
}

export interface ToolGroupMember {
  tool: ToolCall;
  /** 在原 chain 中的下标（`isFinalModel` 之类的链级判定仍用原下标）。 */
  index: number;
}

export type ToolGroupStatus = 'success' | 'failed' | 'running' | 'stopped';

export interface ToolGroup {
  /** 开合状态的稳定键 = 首个成员的 `tool_call_id`（票面指定）。 */
  key: string;
  kind: GroupKind;
  members: ToolGroupMember[];
  status: ToolGroupStatus;
  /** span 起点（首成员 `started_at`）；缺失 = 不知道，不编。 */
  startedAt?: string;
  /** span 终点（末成员 `completed_at`）；缺省 = 末成员仍在跑或时间缺失。 */
  completedAt?: string;
  /** 成员工具名计数（按首现顺序）——组头 `read ×2 · grep ×1` 的事实来源。 */
  names: { name: string; count: number }[];
}

export type ChainEntry =
  | { type: 'node'; node: ChainNode; index: number }
  /** index = 首成员在 chain 中的下标（与 node 分支同一把"原下标"）。 */
  | { type: 'group'; group: ToolGroup; index: number };

/** 组头状态聚合：任一失败→失败；任一运行中→运行；任一被中断→中断；否则完成。 */
export function groupStatus(members: readonly ToolGroupMember[]): ToolGroupStatus {
  let running = false;
  let stopped = false;
  for (const { tool } of members) {
    if (tool.status === 'failed') return 'failed';
    if (tool.status === 'running') running = true;
    else if (tool.status === 'stopped') stopped = true;
  }
  if (running) return 'running';
  if (stopped) return 'stopped';
  return 'success';
}

/** span 口径：首成员开始 → 末成员结束（末成员仍在跑则终点缺省）。 */
export function groupSpan(members: readonly ToolGroupMember[]): {
  startedAt?: string;
  completedAt?: string;
} {
  if (members.length === 0) return {};
  const last = members[members.length - 1].tool;
  return {
    startedAt: members[0].tool.started_at,
    completedAt: last.status === 'running' ? undefined : last.completed_at,
  };
}

/** 成员工具名计数（首现顺序，保持稳定输出便于测试与组头文案）。 */
export function groupNames(members: readonly ToolGroupMember[]): { name: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const { tool } of members) counts.set(tool.name, (counts.get(tool.name) ?? 0) + 1);
  return [...counts].map(([name, count]) => ({ name, count }));
}

function makeGroup(run: readonly { tool: ToolCall; index: number; kind: GroupKind }[]): ToolGroup {
  const members = run.map(({ tool, index }) => ({ tool, index }));
  return {
    key: run[0].tool.tool_call_id,
    kind: run[0].kind,
    members,
    status: groupStatus(members),
    ...groupSpan(members),
    names: groupNames(members),
  };
}

/** 把 chain 折叠成渲染序列。**不修改入参**，顺序与 chain 一一对应。 */
export function groupChain(chain: readonly ChainNode[]): ChainEntry[] {
  const out: ChainEntry[] = [];
  let run: { tool: ToolCall; index: number; kind: GroupKind }[] = [];

  const flush = () => {
    if (run.length >= 2) {
      out.push({ type: 'group', group: makeGroup(run), index: run[0].index });
    } else if (run.length === 1) {
      const { tool, index } = run[0];
      out.push({ type: 'node', node: { kind: 'tool', tool }, index });
    }
    run = [];
  };

  chain.forEach((node, index) => {
    if (node.kind !== 'tool') {
      flush();
      out.push({ type: 'node', node, index });
      return;
    }
    const kind = groupKindOf(node.tool);
    if (kind === null) {
      flush();
      out.push({ type: 'node', node, index });
      return;
    }
    if (run.length > 0 && run[0].kind !== kind) flush();
    run.push({ tool: node.tool, index, kind });
  });
  flush();
  return out;
}
