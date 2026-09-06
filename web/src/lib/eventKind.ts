/** lib/eventKind — RuntimeEventKind 推断层（ADR-0014 D1，PRD §5.2/§10）。
 *
 * 把现有 SessionEvent 投影物（ToolCall / ModelSegment）归类成中间主区的
 * 语义 kind，驱动语义图标 + 类型标签（ZCode 式"动作语言"）。纯函数、
 * view over events——events 仍是唯一事实源（不变量 #22），这里不持有状态。
 *
 * 协议边界：不新增后端 EventType。skill / subagent / todo 当前后端无对应
 * SessionEvent，kind 定义存在（renderer 注册、映射完备）但推断器永不返回
 * 它们——数据不存在就不渲染，不伪造（ADR-0014 D1 预留策略）。
 */

import {
  Bot, Brain, CircleX, Cpu, FilePen, ListChecks, Plug, Search, Sparkles,
  Terminal, Wrench,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { toolEventKey } from './disclosure';

/** 中间主区事件语义分类。 */
export type RuntimeEventKind =
  /** 模型输出段（非终态的已完成段 / 流式段的外显类型） */
  | 'model'
  /** 流式中的模型 burst（🧠 思考中） */
  | 'thinking'
  /** turn 最后一个完成的模型段——高对比正文（PRD §11.2），无图标行 */
  | 'final-answer'
  /** bash / 命令执行 */
  | 'terminal'
  /** read / grep / glob（⌕ 查阅） */
  | 'search'
  /** edit / write / apply_patch（文件写入） */
  | 'write'
  /** mcp__{server}__{tool}（⌁ 连接器工具） */
  | 'mcp'
  /** 其它通用工具（🔧 wrench） */
  | 'tool'
  /** 语义叠加态：任何 kind 的失败工具（错误摘要行 + 状态色，kind 不变） */
  | 'error'
  // ── 以下三类后端当前无事件承载——注册位，推断器永不返回（ADR-0014 D1）──
  | 'skill'
  | 'subagent'
  | 'todo';

/** 工具名 → kind（PRD §10.2 映射 + 现有 toolShapes 的识别逻辑）。
 * 识别顺序即优先级：MCP 前缀最先（mcp__ 名可能撞保留字），随后内置语义组。 */
export function toolKind(name: string): RuntimeEventKind {
  if (name.startsWith('mcp__')) return 'mcp';
  if (name === 'bash') return 'terminal';
  if (name === 'read' || name === 'grep' || name === 'glob') return 'search';
  if (name === 'edit' || name === 'write' || name === 'apply_patch') return 'write';
  return 'tool';
}

/** 模型段 → kind。isFinal = 该段是否为 turn 的最后一个模型段（终态回答）。
 *  streaming 一律 thinking（🧠 思考中…）；done 段按是否终态分 final-answer / model。 */
export function modelKind(status: 'streaming' | 'done', isFinal: boolean): RuntimeEventKind {
  if (status === 'streaming') return 'thinking';
  return isFinal ? 'final-answer' : 'model';
}

/** kind 的状态是否为失败叠加态（PRD §5.2 Error：轻量错误图标 + 摘要行，
 *  不满屏红、kind 本身不变）。 */
export function withError(kind: RuntimeEventKind, failed: boolean): RuntimeEventKind {
  return failed && kind !== 'final-answer' ? 'error' : kind;
}

// ── 视觉映射（PRD §10.2 直译 lucide；16px 语义图标，无圆形底座） ──

export const KIND_ICON: Record<RuntimeEventKind, LucideIcon> = {
  thinking: Brain,
  search: Search,
  terminal: Terminal,
  write: FilePen,
  tool: Wrench,
  mcp: Plug,
  model: Cpu,
  error: CircleX,
  skill: Sparkles,
  subagent: Bot,
  todo: ListChecks,
  // final-answer 渲染为高对比正文，不出现在图标行（PRD §11.2）
  'final-answer': Cpu,
};

/** 类型标签（PRD §3.4 动作语言：终端 / 查阅 / 技能…）。final-answer 无行标签。 */
export const KIND_LABEL: Record<RuntimeEventKind, string> = {
  thinking: '思考',
  search: '查阅',
  terminal: '终端',
  write: '写入',
  tool: '工具',
  mcp: 'MCP',
  model: '输出',
  error: '失败',
  skill: '技能',
  subagent: '子智能体',
  todo: '待办',
  'final-answer': '',
};

/** kind 是否有 L0 图标行（final-answer 直接正文渲染；error 是叠加态，
 *  行图标用原 kind 的，只有状态点/标签变红）。 */
export function hasIconRow(kind: RuntimeEventKind): boolean {
  return kind !== 'final-answer';
}

// ── Main ↔ Inspector 联动（PRD §9，ADR-0014 D5）──

/** 中间主区 DOM 定位锚：data-stream-key（工具行）/ data-step-key（轮次容器）。 */

/** Inspector 事件 → 中间主区定位 key。
 *  工具域事件精确到工具行（复用 disclosure.toolEventKey——同一把 key 同时驱动
 *  L 级 override 与联动定位）；其余事件定位到轮次容器（step:{step_id}）——
 *  模型段在轮内无稳定反推索引，轮次级是诚实粒度。
 *  无 step 且非工具域（session 级事件等）返回 null（无可定位目标）。 */
export function streamKeyFromEvent(
  data: Record<string, unknown>,
  stepId: number | null,
): string | null {
  const toolCallId = data.tool_call_id;
  if (typeof toolCallId === 'string' && toolCallId) return toolEventKey(toolCallId);
  if (stepId !== null) return `step:${stepId}`;
  return null;
}
