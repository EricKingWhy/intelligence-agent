/** F5（#279）三个列表的单次渲染成本探针——`docs/PERF_BASELINE.md` §F5 那几行数字的
 *  **复现脚本**（该节规则要求每条数字可复核：命令 + 脚本路径 + 规模口径）。
 *
 *  口径与 `da01efd` 的 Timeline 实测**同源**（`docs/archive/handoffs/HANDOFF_PERF_FRONTEND.md` P1-4：
 *  「renderToString 探针：20k 全量 359ms / 2k 40ms」）——`renderToString` 全量渲染，量的
 *  是 React 建元素树 + 序列化 HTML 的成本，即「这一屏真要吐多少 HTML」。取中位数
 *  （n=50，预热 10 次排除 JIT）。节点数 = 输出 HTML 里该列表行节点的出现次数。
 *
 *  默认车道已排除 `*.perf.test.ts`（`vitest.config.ts`），不走 `npm test`。跑法：
 *    node node_modules/vitest/vitest.mjs run -c vitest.perf.config.ts \
 *      src/components/stepdetail-list-cost.perf.test.ts
 */
import { describe, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import type { ToolCall } from '../types';
import { initConversation } from '../lib/projection';
import { ArtifactsTab, ChangesTab, ChatTab } from './StepDetail';

/** 该列表有 `n` 项时的工具表。"纯"=只进 TOOLS；diff/artifact 各自只进对应列表。 */
function mkTools(n: number, kind: 'plain' | 'diff' | 'artifact'): ToolCall[] {
  return Array.from({ length: n }, (_, i) => {
    const base: ToolCall = {
      tool_call_id: `tc${i}`,
      name: 'read',
      args: { path: `src/f${i}.ts` },
      status: 'success',
    };
    if (kind === 'diff') {
      return {
        ...base,
        name: 'edit',
        diff: { before: 'const a = 1;\n', after: 'const a = 2;\n', truncated: false },
      };
    }
    if (kind === 'artifact') {
      return {
        ...base,
        name: 'bash',
        artifact: {
          artifact_id: `artifact-${String(i).padStart(8, '0')}`.padEnd(32, '0'),
          size: 4096,
          mime_type: 'text/plain',
          source_tool: 'bash',
        },
      };
    }
    return base;
  });
}

function occurrences(html: string, needle: string): number {
  return html.split(needle).length - 1;
}

const noop = () => {};

/** 中位数（n 次采样，预热 10 次）——口径同 `f1-cost-probe.perf.test.ts`。 */
function bench(label: string, fn: () => string, reps = 50): number {
  let html = '';
  for (let i = 0; i < 10; i++) html = fn();
  const ts: number[] = [];
  for (let i = 0; i < reps; i++) {
    const t0 = performance.now();
    fn();
    ts.push(performance.now() - t0);
  }
  ts.sort((a, b) => a - b);
  const median = ts[reps >> 1];
  console.log(
    `  ${label.padEnd(28)} median=${median.toFixed(3)}ms  bytes=${html.length}  (n=${reps})`,
  );
  return median;
}

describe('F5 探针：三个列表的单次渲染成本 + 节点数', () => {
  it('TOOLS / DIFFS / ARTIFACTS @ N=50/200/500', () => {
    const conversation = initConversation('f5');
    for (const n of [50, 200, 500]) {
      console.log(`N=${n}`);
      const plain = mkTools(n, 'plain');
      const withDiff = mkTools(n, 'diff');
      const withArtifact = mkTools(n, 'artifact');

      bench('TOOLS   ChatTab', () =>
        renderToString(createElement(ChatTab, { conversation, tools: plain, onFocusTool: noop })),
      );
      bench('DIFFS   ChangesTab', () =>
        renderToString(createElement(ChangesTab, { tools: withDiff, sessionId: 'f5' })),
      );
      bench('ARTIFACTS ArtifactsTab', () =>
        renderToString(createElement(ArtifactsTab, { tools: withArtifact, sessionId: 'f5' })),
      );

      // 节点数（同一次全量渲染的 HTML）——判据用，单独打印，不参与计时。
      const htmlTools = renderToString(
        createElement(ChatTab, { conversation, tools: plain, onFocusTool: noop }),
      );
      const htmlDiffs = renderToString(
        createElement(ChangesTab, { tools: withDiff, sessionId: 'f5' }),
      );
      const htmlArtifacts = renderToString(
        createElement(ArtifactsTab, { tools: withArtifact, sessionId: 'f5' }),
      );
      console.log(
        `  rows: TOOLS=${occurrences(htmlTools, 'detail-tool-row')}` +
          ` DIFFS=${occurrences(htmlDiffs, 'class="detail-section"')}` +
          ` ARTIFACTS=${occurrences(htmlArtifacts, 'class="detail-section"')}`,
      );
    }
    // 本用例 ≈58s（三个列表 × 三个规模 × 60 次渲染，实测）——vitest 默认 5s 超时会把它
    // 误报成失败；这是**耗时**不是**挂死**（时长与数字都随机器漂移）。
  }, 180_000);
});
