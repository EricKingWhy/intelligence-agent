/** F1（#270）G3 基线成本探针——`docs/PERF_BASELINE.md` §F1 那几行「单次成本」数字的
 *  **复现脚本**（该节规则要求每条数字可复核：命令 + 脚本路径 + 规模口径）。与仓内既有的
 *  `projection.perf.test.ts` / `streaming.perf.test.ts` 同属 perf 车道，默认车道已在
 *  `vitest.config.ts` 里排除 `*.perf.test.ts`，故不影响 `npm test`。跑法：
 *    node node_modules/vitest/vitest.mjs run -c vitest.perf.config.ts src/lib/f1-cost-probe.perf.test.ts
 *  口径：单次调用耗时取中位数（脚本内 N=200，排除首调用 JIT 预热），量的是
 *  「每次多余重渲染实际烧掉的那部分工作」：markdown 解析 + 执行链推导。
 *  ⚠ 本文件只报数字、**不做阈值断言**：耗时的绝对值随机器漂移，预算断言留给独立车道里
 *  已有的比例型用例。它存在是为了让 F1 的单次成本可被别人用同一条命令复现。
 */
import { describe, it } from 'vitest';
import { renderMarkdown } from './markdown';
import { deriveChain } from './projection';
import type { Turn } from '../types';

function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

/** 一段「长回答」量级的 markdown（≈2k 字符，含标题/列表/强调/行内码/围栏）。 */
function longAnswer(chars: number): string {
  const block = [
    '## 结论',
    '',
    '这段回答包含 **加粗**、`行内代码` 与普通文本，用于模拟真实模型输出。',
    '',
    '- 第一条要点：解析器要处理列表聚合',
    '- 第二条要点：`code` 与 **bold** 混排',
    '- 第三条要点：段落与空行',
    '',
    '```ts',
    'const a = 1;',
    'export function f(x: number) { return x + a; }',
    '```',
    '',
    '### 小节',
    '',
    '再来一段正文，确保块结构足够多样。',
    '',
  ].join('\n');
  let out = '';
  while (out.length < chars) out += block;
  return out.slice(0, chars);
}

const ANSWER = longAnswer(2000);

/** 一个含 3 个 model 段 + 2 个工具的已完成轮（代表"长回答 + 多工具"场景）。 */
const TURN: Turn = {
  step_id: 1,
  user_message: 'hi',
  model: { text: ANSWER, status: 'done' },
  segments: [
    { text: ANSWER.slice(0, 700), status: 'done' },
    { text: ANSWER.slice(0, 1400), status: 'done' },
    { text: ANSWER, status: 'done' },
  ],
  tools: [
    { tool_call_id: 'c1', name: 'bash', args: { command: 'ls' }, status: 'success', result: { ok: true } },
    { tool_call_id: 'c2', name: 'read', args: { path: 'a.ts' }, status: 'success', result: { ok: true } },
  ],
  activities: [
    { kind: 'model', index: 0 },
    { kind: 'tool', tool_call_id: 'c1' },
    { kind: 'model', index: 1 },
    { kind: 'tool', tool_call_id: 'c2' },
    { kind: 'model', index: 2 },
  ],
  status: 'done',
  turn_index: 1,
  user_message_seq: 1,
};

function bench(label: string, fn: () => void, n = 200) {
  for (let i = 0; i < 20; i++) fn(); // 预热
  const ts: number[] = [];
  for (let i = 0; i < n; i++) {
    const t0 = performance.now();
    fn();
    ts.push(performance.now() - t0);
  }
  console.log(
    `${label}: median=${median(ts).toFixed(3)}ms  p95=${[...ts].sort((a, b) => a - b)[Math.floor(n * 0.95)].toFixed(3)}ms  (n=${n})`,
  );
}

describe('F1 成本探针（基线复现脚本）', () => {
  it('renderMarkdown / deriveChain 单次耗时', () => {
    console.log(`markdown 长度 = ${ANSWER.length} 字符；轮含 3 model 段 + 2 工具`);
    bench('renderMarkdown(2k 字长回答)', () => {
      renderMarkdown(ANSWER);
    });
    bench('deriveChain(3 段 + 2 工具)', () => {
      deriveChain(TURN);
    });
    bench('一次多余重渲染合计（解析 + 推导）', () => {
      renderMarkdown(ANSWER);
      deriveChain(TURN);
    });
    // 可见已完成段数 K=3 时，一次提交要做的解析次数
    bench('一次提交 = 3 个可见已完成段全量重解析', () => {
      renderMarkdown(ANSWER.slice(0, 700));
      renderMarkdown(ANSWER.slice(0, 1400));
      renderMarkdown(ANSWER);
    });
  });
});
