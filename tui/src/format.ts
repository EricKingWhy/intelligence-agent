/**
 * 格式化工具：与 Python CLI（src/agent_harness/cli.py 的 _collapse_args /
 * _format_tokens / _dimension_remaining）同一份事实的同一渲染语义。
 * 两个入口各写一套形状规则就会在"同一个数字两种读法"上漂移。
 */

/** 一行折叠工具参数：key=value，字符串含空格才加引号；整体超限截断。 */
export function collapseArgs(args: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(args)) {
    let text: string;
    if (typeof value === "string") {
      text = value.includes(" ") || value === "" ? `"${value}"` : value;
    } else if (typeof value === "object" && value !== null) {
      text = JSON.stringify(value);
    } else {
      text = String(value);
    }
    parts.push(`${key}=${text}`);
  }
  let line = parts.join(" ");
  if (line.length > ARGS_LINE_LIMIT) line = line.slice(0, ARGS_LINE_LIMIT) + "...";
  return line;
}

const ARGS_LINE_LIMIT = 200;

/** token 数 -> 紧凑文本（K/M 压缩）；不可得读数返回 ?（不编数）。 */
export function formatTokens(count: unknown): string {
  if (typeof count !== "number" || !Number.isInteger(count) || count < 0) return "?";
  if (count < 1000) return String(count);
  if (count < 1_000_000) return `${(count / 1000).toFixed(1)}k`;
  return `${(count / 1_000_000).toFixed(1)}M`;
}

/** 定点十进制解析：[整数字段][.小数字段] -> { digits(带符号 BigInt), scale }；失败 null。 */
function parseDecimal(value: string): { digits: bigint; scale: number } | null {
  if (!/^-?\d+(\.\d+)?$/.test(value)) return null;
  const negative = value.startsWith("-");
  const body = negative ? value.slice(1) : value;
  const [intPart, fracPart = ""] = body.split(".");
  const digits = BigInt((negative ? "-" : "") + intPart + fracPart);
  return { digits, scale: fracPart.length };
}

/** 统一 scale 后做整数减法，避免二进制浮点近似（wire 上 cost 是十进制字符串）。 */
export function decimalRemaining(
  consumed: unknown,
  ceiling: unknown,
): string {
  if (ceiling === null || ceiling === undefined || ceiling === "") return "unavailable";
  if (consumed === null || consumed === undefined || consumed === "") return "unavailable";
  if (typeof ceiling === "boolean" || typeof consumed === "boolean") return "unavailable";
  if (typeof ceiling === "number" && typeof consumed === "number") {
    if (!Number.isInteger(consumed) || !Number.isInteger(ceiling) || ceiling < 0) {
      return "unavailable";
    }
    return String(Math.max(ceiling - consumed, 0));
  }
  if (typeof consumed !== "string" && typeof consumed !== "number") return "unavailable";
  if (typeof ceiling !== "string" && typeof ceiling !== "number") return "unavailable";
  const left = parseDecimal(String(ceiling));
  const right = parseDecimal(String(consumed));
  if (left === null || right === null) return "unavailable";
  const scale = Math.max(left.scale, right.scale);
  const scaleUp = (v: { digits: bigint; scale: number }) =>
    v.digits * 10n ** BigInt(scale - v.scale);
  const diff = scaleUp(left) - scaleUp(right);
  if (diff <= 0n) return "0";
  let text = diff.toString().padStart(scale + 1, "0");
  if (scale > 0) {
    const cut = text.length - scale;
    text = text.slice(0, cut) + "." + text.slice(cut);
    text = text.replace(/0+$/, "").replace(/\.$/, "");
  }
  return text;
}
