/** 行级 unified diff —— Inspector「变更」面的展开视图（#1419 C-3）。
 *
 *  输入是工具卡已有的 `diff.before` / `diff.after` 两份**文本**，输出带行号的
 *  unified 行表（`@@` 段头 + 上下文 3 行）。不引入 diff 库：这一处只需要
 *  逐行 LCS，几十行代码就够，且行为可以单测钉住。
 *
 *  **规模闸**：LCS 的代价是 O(n·m) 格。超过 `LINE_DIFF_MAX_CELLS` 就退化成
 *  `mode: 'whole'`——调用方据此回到「整段对照」（既有 `DiffBlock` 的双栏），
 *  并且**不给 +/− 计数**：那两个数字必须是真的，算不出来就不显示。
 */

export type DiffRowKind = 'ctx' | 'add' | 'del' | 'hunk';

export interface DiffRow {
  kind: DiffRowKind;
  /** 变更前的行号（`add` 行为 null）。 */
  left: number | null;
  /** 变更后的行号（`del` 行为 null）。 */
  right: number | null;
  /** 行文本（`hunk` 行是段头原文）。 */
  text: string;
}

export interface LineDiffResult {
  /** `unified` = 行表可用；`whole` = 超出规模闸，只有整段对照。 */
  mode: 'unified' | 'whole';
  rows: DiffRow[];
  /** 新增/删除行数；`whole` 模式下为 null（算不出来就不报数）。 */
  added: number | null;
  removed: number | null;
}

/** LCS 表格的格数上限（约 1000×1000 行）。超过即降级。 */
export const LINE_DIFF_MAX_CELLS = 1_000_000;

/** unified diff 的上下文行数（与 `diff -u` 默认一致）。 */
export const LINE_DIFF_CONTEXT = 3;

/** 行表里连续变化的区段两侧各留多少行上下文。 */
type Op = 'eq' | 'add' | 'del';

function splitLines(text: string): string[] {
  if (text === '') return [];
  const lines = text.split('\n');
  // 结尾换行不产生一条空行（`"a\n"` 是一行，不是两行）。
  if (lines[lines.length - 1] === '') lines.pop();
  return lines;
}

/** 逐行 LCS → 编辑脚本。表格超限时返回 null（调用方降级）。 */
function editScript(a: string[], b: string[]): Op[] | null {
  const n = a.length;
  const m = b.length;
  if (n === 0) return b.map(() => 'add' as Op);
  if (m === 0) return a.map(() => 'del' as Op);
  if (n * m > LINE_DIFF_MAX_CELLS) return null;

  // dp[i][j] = a[i:] 与 b[j:] 的 LCS 长度，压成一维逐行回填。
  const width = m + 1;
  const dp = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * width + j] = a[i] === b[j]
        ? dp[(i + 1) * width + j + 1] + 1
        : Math.max(dp[(i + 1) * width + j], dp[i * width + j + 1]);
    }
  }
  const ops: Op[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push('eq');
      i++;
      j++;
    } else if (dp[(i + 1) * width + j] >= dp[i * width + j + 1]) {
      ops.push('del');
      i++;
    } else {
      ops.push('add');
      j++;
    }
  }
  while (i < n) {
    ops.push('del');
    i++;
  }
  while (j < m) {
    ops.push('add');
    j++;
  }
  return ops;
}

/**
 * 计算行级 unified diff。
 *
 * 段头只出现在**被省略的上下文**之间；整文件都在一个上下文窗口内时没有 `@@` 行
 * （与 `git diff --unified` 对整文件替换的输出一致，也比凭空造一个 `@@` 诚实）。
 */
export function lineDiff(before: string, after: string): LineDiffResult {
  const a = splitLines(before);
  const b = splitLines(after);
  const ops = editScript(a, b);
  if (ops === null) return { mode: 'whole', rows: [], added: null, removed: null };

  let added = 0;
  let removed = 0;
  for (const op of ops) {
    if (op === 'add') added++;
    else if (op === 'del') removed++;
  }

  // 带行号的完整行表（先不做上下文裁剪）。
  const full: DiffRow[] = [];
  let li = 0;
  let ri = 0;
  let ai = 0;
  let bi = 0;
  for (const op of ops) {
    if (op === 'eq') {
      full.push({ kind: 'ctx', left: li + 1, right: ri + 1, text: a[ai] });
      li++;
      ri++;
      ai++;
      bi++;
    } else if (op === 'del') {
      full.push({ kind: 'del', left: li + 1, right: null, text: a[ai] });
      li++;
      ai++;
    } else {
      full.push({ kind: 'add', left: null, right: ri + 1, text: b[bi] });
      ri++;
      bi++;
    }
  }

  /* 没有变化（两份文本逐行相同）：整份都是上下文，不做裁剪——否则「全是 ctx」
   * 的 keep 表全 0，行表会被裁成空的（空 diff 卡比原样展示更误导）。 */
  if (added === 0 && removed === 0) {
    return { mode: 'unified', rows: full, added, removed };
  }

  // 上下文裁剪：保留每个变化行 ± CONTEXT 行，相邻区间合并。
  const keep = new Uint8Array(full.length);
  for (let i = 0; i < full.length; i++) {
    if (full[i].kind === 'ctx') continue;
    const from = Math.max(0, i - LINE_DIFF_CONTEXT);
    const to = Math.min(full.length - 1, i + LINE_DIFF_CONTEXT);
    for (let k = from; k <= to; k++) keep[k] = 1;
  }
  const rows: DiffRow[] = [];
  /** 被省略区段之前的最后一个行号，供「段头起点」在块首是纯增/纯删时兜底。 */
  let lastLeft = 0;
  let lastRight = 0;
  let cursor = 0;
  while (cursor < full.length) {
    if (!keep[cursor]) {
      let next = cursor;
      while (next < full.length && !keep[next]) next++;
      if (next < full.length) {
        // 段头：起点取块内第一个非空行号，块首是纯增/纯删时退到「上一行 + 1」。
        let oldStart: number | null = null;
        let newStart: number | null = null;
        let oldCount = 0;
        let newCount = 0;
        for (let k = next; k < full.length && keep[k]; k++) {
          if (full[k].left !== null) {
            oldCount++;
            if (oldStart === null) oldStart = full[k].left;
          }
          if (full[k].right !== null) {
            newCount++;
            if (newStart === null) newStart = full[k].right;
          }
        }
        rows.push({
          kind: 'hunk',
          left: null,
          right: null,
          text: `@@ -${oldStart ?? lastLeft + 1},${oldCount} +${newStart ?? lastRight + 1},${newCount} @@`,
        });
      }
      cursor = next;
      continue;
    }
    if (full[cursor].left !== null) lastLeft = full[cursor].left!;
    if (full[cursor].right !== null) lastRight = full[cursor].right!;
    rows.push(full[cursor]);
    cursor++;
  }

  return { mode: 'unified', rows, added, removed };
}
