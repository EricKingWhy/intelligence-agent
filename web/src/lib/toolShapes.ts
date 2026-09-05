/** lib/toolShapes — 工具结果新形状的纯解析（后端 df4f7d8 硬化批次 §1.3）。
 *
 * 后端在 ToolResult.data 里用文本标记表达截断/续读语义；这里把标记解析成
 * 结构化事实，渲染层（ToolCard）据此出提示。纯函数、可测、零伪造——标记
 * 不匹配就返回 null 回退通用渲染，绝不猜。
 */

export interface ReadContinuation {
  /** 本次已显示的行范围（1-based，含端点）。 */
  shownFrom: number;
  shownTo: number;
  /** 文件总行数（后端 data.total_lines 真值）。 */
  totalLines: number;
  /** 续读起始 offset（= shownTo + 1，read 的 1-based 参数）。 */
  nextOffset: number;
}

export interface ReadShape {
  /** 去掉尾部标记后的正文（无标记时即原文）。 */
  content: string;
  /** 后端 data.total_lines（缺失为 null——不伪造）。 */
  totalLines: number | null;
  /** 续读标记解析结果（无标记 null）。 */
  continuation: ReadContinuation | null;
  /** 单行超长截断标记（不可续读）：{ line, bytes } 或 null。 */
  lineTruncated: { line: number; bytes: number } | null;
}

const CONTINUATION_RE = /\[Showing lines (\d+)-(\d+) of (\d+)\. Use offset=(\d+) to continue\.\]\s*$/;
const LINE_TRUNCATED_RE = /\[Line (\d+) truncated at (\d+) bytes[^\]]*\]\s*$/;

/** 解析 read 工具的 data（{ content?, total_lines? }）。形状不符返回 null。 */
export function parseReadShape(data: unknown): ReadShape | null {
  if (typeof data !== 'object' || data === null) return null;
  const d = data as Record<string, unknown>;
  if (typeof d.content !== 'string') return null;
  const content = d.content;
  const totalLines = typeof d.total_lines === 'number' && Number.isFinite(d.total_lines) ? d.total_lines : null;

  const contMatch = CONTINUATION_RE.exec(content);
  let continuation: ReadContinuation | null = null;
  let body = content;
  if (contMatch) {
    const shownFrom = Number(contMatch[1]);
    const shownTo = Number(contMatch[2]);
    const total = Number(contMatch[3]);
    const nextOffset = Number(contMatch[4]);
    if ([shownFrom, shownTo, total, nextOffset].every(Number.isFinite)) {
      continuation = { shownFrom, shownTo, totalLines: total, nextOffset };
      body = content.slice(0, contMatch.index).replace(/\n$/, '');
    }
  }

  let lineTruncated: ReadShape['lineTruncated'] = null;
  const lineMatch = LINE_TRUNCATED_RE.exec(body);
  if (lineMatch) {
    const line = Number(lineMatch[1]);
    const bytes = Number(lineMatch[2]);
    if (Number.isFinite(line) && Number.isFinite(bytes)) {
      lineTruncated = { line, bytes };
      body = body.slice(0, lineMatch.index).replace(/\n$/, '');
    }
  }

  return { content: body, totalLines, continuation, lineTruncated };
}

/** grep 匹配行尾标记（单行 >500 字符被后端截断）。 */
export const GREP_TRUNCATED_SUFFIX = '... [truncated]';

/** 行是否以截断尾巴结束（渲染层把尾巴弱化为静音标记）。 */
export function hasGrepTruncatedSuffix(line: string): boolean {
  return line.endsWith(GREP_TRUNCATED_SUFFIX);
}

/** 剥离行尾截断尾巴，返回正文（无尾巴时原样返回）。
 *  标记语义归 toolShapes 单点所有——渲染层不再手写 slice 偏移。 */
export function stripGrepTruncatedSuffix(line: string): string {
  return hasGrepTruncatedSuffix(line) ? line.slice(0, -GREP_TRUNCATED_SUFFIX.length) : line;
}

// ── da394a9 批：diff 归档 marker / MCP 工具名 ──

const INSPECT_ARTIFACT_RE = /use inspect_artifact\(([^)]+)\)/;

/** 从 diff 截断摘要中提取归档 artifact id（后端 >2000 字符时内嵌 marker）。
 *  无 marker 返回 null——零伪造，不猜 id。 */
export function parseArtifactMarker(text: string): string | null {
  const m = INSPECT_ARTIFACT_RE.exec(text);
  const id = m?.[1]?.trim();
  return id ? id : null;
}

export interface McpToolName {
  server: string;
  tool: string;
}

/** 拆解 MCP 工具名 mcp__{server}__{tool}（da394a9 Phase 8）。
 *  非 MCP 名返回 null；tool 部分可能含下划线，以首个 `__` 后界分段：
 *  mcp__github__list_issues → { server: 'github', tool: 'list_issues' }。 */
export function splitMcpToolName(name: string): McpToolName | null {
  if (!name.startsWith('mcp__')) return null;
  const rest = name.slice('mcp__'.length);
  const sep = rest.indexOf('__');
  if (sep <= 0) return null;
  const server = rest.slice(0, sep);
  const tool = rest.slice(sep + 2);
  if (!server || !tool) return null;
  return { server, tool };
}
