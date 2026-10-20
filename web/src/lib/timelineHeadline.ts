/**
 * #1419 T-8：时间线「人话标题」。
 *
 * 效果图 `.fc-title` 不是"工具名 + 参数"，而是模板推导的一句话：
 *   `读 sessions.py 的 118–204 行` / `在 src/api 里找 list_sessions` /
 *   `跑 pytest tests/api` / `改 serializers.py：空 payload 兜底`。
 *
 * 约束（票面）：纯函数、确定性、可单测；**只用事件里已有的字段**，缺字段就退回
 * 更短但仍真实的形态（`读 foo.py`），不猜、不编造。
 *
 * 与 `summarizeEvent`（projection）的分工：那边是"事件摘要"（中心列也用），这里
 * 是 Inspector 时间线的标题模板——只在本面用，所以单独成模块，避免改 projection
 * 的公共语义。
 */
import { EventType } from '../types';
import type { AgentEvent, ToolCall } from '../types';

/** 标题里的文件只留 basename：效果图是短名，完整路径在展开区可见。 */
function baseName(path: string): string {
  const trimmed = path.trim();
  if (!trimmed) return '';
  const parts = trimmed.split(/[\\/]+/);
  return parts[parts.length - 1] || trimmed;
}

function firstString(data: Record<string, unknown>, keys: readonly string[]): string | null {
  for (const key of keys) {
    const value = data[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

function firstLine(text: string, max = 48): string {
  const line = text.split('\n').find((l) => l.trim())?.trim() ?? '';
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** 千分位（不依赖 locale：SSR 与浏览器必须逐字一致）。 */
export function formatCount(n: number): string {
  const s = String(Math.trunc(Math.abs(n)));
  const grouped = s.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return n < 0 ? `-${grouped}` : grouped;
}

const FILE_KEYS = ['path', 'file_path', 'filepath', 'filename', 'file', 'target_file', 'abs_path'] as const;
const PATTERN_KEYS = ['pattern', 'query', 'regex', 'q', 'search'] as const;
const DIR_KEYS = ['path', 'dir', 'directory', 'cwd', 'root'] as const;

/** 读类工具的行范围：`offset` 是起始行（1 起），`limit` 是行数。 */
function toolCallTitle(name: string, args: Record<string, unknown>): string {
  const tool = name.toLowerCase();
  const file = firstString(args, FILE_KEYS);
  if (/^(read|read_file|read_artifact|inspect_artifact|view|cat)$/.test(tool)) {
    if (!file) return '读文件';
    const offset = finiteNumber(args.offset) ?? finiteNumber(args.start_line);
    const limit = finiteNumber(args.limit) ?? finiteNumber(args.line_count);
    const range = offset != null && limit != null && limit > 0
      ? ` 的 ${offset}–${offset + limit - 1} 行`
      : offset != null ? ` 从第 ${offset} 行` : '';
    return `读 ${baseName(file)}${range}`;
  }
  if (/^(grep|search|search_files|rg|glob|find|list_dir|ls)$/.test(tool)) {
    const pattern = firstString(args, PATTERN_KEYS);
    const dir = firstString(args, DIR_KEYS);
    if (pattern && dir) return `在 ${baseName(dir)} 里找 ${firstLine(pattern, 28)}`;
    if (pattern) return `找 ${firstLine(pattern, 36)}`;
    if (dir) return `看 ${baseName(dir)}`;
    return '检索';
  }
  if (/^(bash|shell|sh|run_command|exec|execute|terminal|run)$/.test(tool)) {
    const cmd = firstString(args, ['command', 'cmd', 'script', 'input']);
    return cmd ? `跑 ${firstLine(cmd, 44)}` : '跑命令';
  }
  if (/^(edit|apply_patch|str_replace|replace|patch|multiedit)$/.test(tool)) {
    if (!file) return '改文件';
    const after = typeof args.new_string === 'string' ? args.new_string : typeof args.content === 'string' ? args.content : null;
    const gist = after ? firstLine(after, 28) : '';
    return gist ? `改 ${baseName(file)}：${gist}` : `改 ${baseName(file)}`;
  }
  if (/^(write|write_file|create_file|create)$/.test(tool)) {
    if (!file) return '写文件';
    const content = typeof args.content === 'string' ? args.content : null;
    const lines = content === null ? null : content.split('\n').length;
    return lines === null ? `写 ${baseName(file)}` : `写 ${baseName(file)}，${lines} 行`;
  }
  if (/^(delegate|spawn|task|subagent|agent)$/.test(tool)) {
    const task = firstString(args, ['task', 'prompt', 'instruction', 'description', 'query']);
    return task ? `派子会话：${firstLine(task, 40)}` : '派子会话';
  }
  // 未知工具：名字 + 第一个标量参数（真实数据，不猜语义）。
  const scalar = Object.entries(args).find(([, v]) => typeof v === 'string' && v.trim());
  return scalar ? `${name} · ${firstLine(String(scalar[1]), 36)}` : name;
}

/** TOOL_RESULT 的失败原因：优先后端 `error_code`，其次 `message`，再次 `exit_code`。
 *  返回 null = **内容本身不表达失败**。
 *
 *  ⚠ 判据只有一条：信封里 `ok === false`。裸文本、JSON 标量（数字/字符串/`null`）、
 *  以及 `ok` 缺席的信封一律返回 null——"结果里有文字"不等于"结果失败了"，把首行
 *  文本当失败原因会让每条成功的纯文本输出都显示成「失败：<stdout 首行>」。真失败
 *  但信封没有 `ok` 时，调用方按 `call.status === 'failed'` 取内容首行兜底。 */
function failureReason(content: unknown): string | null {
  if (typeof content !== 'string') return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
  const envelope = parsed as Record<string, unknown>;
  if (envelope.ok !== false) return null;
  const code = typeof envelope.error_code === 'string' ? envelope.error_code : null;
  const message = typeof envelope.message === 'string' ? envelope.message : null;
  if (code && message) return `${code} · ${firstLine(message, 28)}`;
  if (code) return code;
  if (message) return firstLine(message, 36);
  return '无错误详情';
}

/** 命令的退出码（`tool.result.exit_code`）：bash 失败时标题优先用它——`exit 2` 比
 *  「失败：<stderr 首行>」更能定位问题，两者都在时并列。 */
function resultExitCode(call: ToolCall | null | undefined): number | null {
  const result = call?.result;
  if (typeof result !== 'object' || result === null) return null;
  const code = (result as Record<string, unknown>).exit_code;
  return typeof code === 'number' && Number.isFinite(code) ? code : null;
}

export interface TimelineHeadline {
  title: string;
  /** 副行额外细节（模型用量等）；无真实数据时为 null，不占位。 */
  note: string | null;
}

/**
 * 事件标题 + 副行细节。
 * @param event 事件本身
 * @param call  TOOL_RESULT 对应的 TOOL_CALL（有则标题取 call 的摘要 + 结果要旨）
 */
export function timelineHeadline(event: AgentEvent, call?: ToolCall | null): TimelineHeadline {
  const data = event.data;
  switch (event.type) {
    case EventType.TOOL_CALL: {
      const name = typeof data.tool_name === 'string' ? data.tool_name : '';
      const args = typeof data.args === 'object' && data.args !== null ? (data.args as Record<string, unknown>) : {};
      return { title: name ? toolCallTitle(name, args) : '工具调用', note: null };
    }
    case EventType.TOOL_RESULT: {
      const callName = call?.name ?? (typeof data.tool_name === 'string' ? data.tool_name : '');
      const callArgs = call?.args ?? {};
      const base = callName ? toolCallTitle(callName, callArgs) : '工具返回';
      const reason = failureReason(data.content);
      if (reason !== null || call?.status === 'failed') {
        const exitCode = resultExitCode(call);
        const detail = reason ?? (typeof data.content === 'string' ? firstLine(data.content, 36) : '');
        /* 退出码优先：`exit 2` 是后端给的确切事实，比 stderr 首行更可定位。
           两者都有时并列（`exit 2 · <首行>`），不丢信息。 */
        const parts = [
          exitCode !== null ? `exit ${exitCode}` : null,
          detail || null,
        ].filter((part): part is string => Boolean(part));
        const tail = parts.length > 0 ? `：${parts.join(' · ')}` : '';
        return { title: `${base} · 失败${tail}`, note: null };
      }
      if (call?.status === 'stopped') return { title: `${base} · 已中断`, note: null };
      return { title: `${base} · 完成`, note: null };
    }
    case EventType.MODEL_COMPLETED: {
      const model = typeof data.model === 'string' && data.model ? data.model : null;
      const usage = typeof data.usage === 'object' && data.usage !== null ? (data.usage as Record<string, unknown>) : null;
      const prompt = usage ? finiteNumber(usage.prompt_tokens) : null;
      const completion = usage ? finiteNumber(usage.completion_tokens) : null;
      const note =
        prompt !== null || completion !== null
          ? `${prompt !== null ? formatCount(prompt) : '—'} in / ${completion !== null ? formatCount(completion) : '—'} out`
          : null;
      return { title: model ? `模型生成完成 · ${model}` : '模型生成完成', note };
    }
    case EventType.MODEL_REQUEST: {
      const model = typeof data.model === 'string' && data.model ? data.model : null;
      return { title: model ? `请求模型 · ${model}` : '请求模型', note: null };
    }
    case EventType.MODEL_STARTED:
    case EventType.MODEL_REQUEST_STARTED: {
      const model = typeof data.model === 'string' && data.model ? data.model : null;
      return { title: model ? `模型开始生成 · ${model}` : '模型开始生成', note: null };
    }
    case EventType.MODEL_FAILED: {
      const reason = firstString(data, ['error', 'message', 'error_code']);
      return { title: reason ? `模型调用失败 · ${firstLine(reason, 32)}` : '模型调用失败', note: null };
    }
    case EventType.RUN_STARTED: {
      const model = typeof data.model === 'string' && data.model ? data.model : null;
      return { title: model ? `任务开始 · ${model}` : '任务开始', note: null };
    }
    case EventType.SESSION_STARTED:
      return { title: '会话开始', note: null };
    case EventType.SESSION_RESUMED:
      return { title: '会话恢复', note: null };
    case EventType.USER_INPUT_REQUESTED:
      return { title: '等待用户输入', note: null };
    default:
      return { title: '', note: null };
  }
}
