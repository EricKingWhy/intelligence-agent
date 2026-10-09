/**
 * 待发图片数组 + `[Image #N]` 标记（AC3 的状态机，纯函数）。
 *
 * **PORT DESIGN**（语义移植，不复制代码）：oh-my-pi
 * `packages/tui/src/prompt/composer-attachments.ts:273 compactImageMarkers`
 * @ commit `579da1d661c5cb8d43bc2ddd429ab72e67165ad8`（MIT，见 `tui/THIRD_PARTY_NOTICES.md`）。
 * 上游另有一路 Cline 语义（`apps/cli/src/tui/hooks/use-prompt-input-controller.ts:149-153`
 * @ `cd80a20e`，Apache-2.0）：标记号 = `数组长度 + 1`，删标记只过滤、**不重编号**。
 * 本仓取 oh-my-pi 那一支，理由（阶段一已定）：标记是**位置式**的（`[Image #N]` 对应
 * `images[N-1]`），提交时稠密重编号 => 序号恒等于下标，天然不会出现 Cline 的重号/错位。
 *
 * 交互契约（AC3，也是本文件唯一承诺的外部行为）：
 * - 插入一张图 => 末尾追加 `[Image #N]`（N = 插入后数组长度），数组 push；
 * - 用户在编辑器里删掉某个标记 => 提交时该图**不进**提交内容（数组被过滤）；
 * - 提交时仍被引用的图按"原下标升序"重编号为 1..K，文本同步重写；
 * - 引用不到任何保留图的**悬空标记**（手打 `[Image #99]`、超界序号）在提交时删掉，
 *   绝不原样发给模型（独立审查 P4：悬空标记进正文等于给模型一个不存在的引用）。
 */

/** 一张待发图片（字节流式上传，**不**上行 base64；见 MM-02 契约）。 */
export interface PendingImage {
  bytes: Uint8Array;
  mimeType: string;
  /** 展示名（文件名；剪贴板字节没有路径时用它，不编造路径）。 */
  name: string;
  /** 来源文件绝对路径（已知才有；AC6 的"可打开的原图路径"用它）。 */
  path: string | null;
}

/** 标记序号上限：与上游同形（`[1-9]\d*`），避免把 `[Image #0]` 当成合法引用。 */
const IMAGE_MARKER_PATTERN = /\[Image #([1-9]\d*)\]/g;

/** 带前导行内空白的标记：删悬空标记时要把那段空白一起吞掉（`a [Image #99] b` => `a b`）。 */
const MARKER_WITH_LEADING_BLANKS = /([ \t]*)\[Image #([1-9]\d*)\]/g;

/** `[Image #N]` 标记文本（N 从 1 起）。 */
export function imageMarker(n: number): string {
  return `[Image #${n}]`;
}

/** 文本里出现的标记序号，按出现顺序（允许重复）。 */
export function parseImageMarkers(text: string): number[] {
  const found: number[] = [];
  const scanner = new RegExp(IMAGE_MARKER_PATTERN.source, "g");
  for (;;) {
    const match = scanner.exec(text);
    if (match === null) break;
    found.push(Number(match[1]));
  }
  return found;
}

/**
 * 提交时的稠密重编号：`keep` = 保留下来的**原数组下标**（升序），`text` 里仍被引用的
 * 标记按 `keep` 重写；引用不到保留图的**悬空标记**删掉（连同它前面的行内空白；
 * 整行只剩悬空标记时连空行一起删，不留空行残留）。
 * 无需任何改写（全部引用且已是 1..K、且没有悬空标记）时返回 `null` -- 调用方据此跳过
 * 一切改写，纯文本提交逐字不变。
 */
export function compactDraftImages(
  text: string,
  imageCount: number,
): { text: string; keep: number[] } | null {
  if (imageCount === 0) return null;
  const markers = parseImageMarkers(text);
  // keep 收的是**标记序号**（1 起），与文本里的 `[Image #N]` 同口径；返回时再转成 0 起下标。
  const referenced = new Set<number>();
  for (const n of markers) {
    if (n <= imageCount) referenced.add(n);
  }
  const keep = [...referenced].sort((a, b) => a - b);
  const dense = keep.length === imageCount && keep.every((n, i) => n === i + 1);
  if (dense && !markers.some((n) => n > imageCount)) return null;
  const lines = text.split("\n");
  const rewritten = lines
    .map((line) =>
      line.replace(MARKER_WITH_LEADING_BLANKS, (_match, blanks: string, idx: string) => {
        const position = keep.indexOf(Number(idx));
        return position === -1 ? "" : blanks + imageMarker(position + 1);
      }),
    )
    // 整行只剩被删掉的悬空标记时连行一起删（用户原本就有的空行原样保留）。
    .filter((line, index) => !(line.trim() === "" && (lines[index] ?? "").trim() !== ""));
  return { text: rewritten.join("\n"), keep: keep.map((n) => n - 1) };
}

/**
 * 抹掉文本里的全部 `[Image #N]` 标记（不是"按事件重编号"，而是清场）。
 * 用途：切会话时旧会话的待发图不许跟着进新会话（#827 独立审查 P3）。
 */
export function stripImageMarkers(text: string): string {
  return text.replace(IMAGE_MARKER_PATTERN, "");
}
