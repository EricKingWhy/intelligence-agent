/** #189 中心列「文件/改动」面的 SSR 结构契约。
 *
 *  交互（点文件名 → 右侧换 diff）在 `e2e/z-changes-panel.spec.ts`；这里锁结构，因为
 *  本票最容易失守的几条都是**结构**："一个文件一行"（聚合了没有）、"复用 DiffBlock"
 *  （是不是又写了一套 diff 渲染）、"只读"（有没有混进输入类元素）、"不可得就不给数"
 *  （归档/截断时有没有编一个统计）。
 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { EventType, type AgentEvent, type ToolCall } from '../types';
import { changedFiles } from '../lib/changedFiles';
import { ChangesPanel } from './ChangesPanel';

function editTool(
  id: string,
  path: string,
  before: string,
  after: string,
  over: Partial<ToolCall> = {},
): ToolCall {
  return {
    tool_call_id: id,
    name: 'edit',
    args: { path },
    status: 'success',
    result: { ok: true },
    diff: { before, after, truncated: false },
    ...over,
  };
}

const render = (tools: readonly ToolCall[], over: Record<string, unknown> = {}) =>
  renderToString(
    createElement(ChangesPanel, { ...changedFiles(tools), eventsVersion: 0, ...over }),
  ).replaceAll('<!-- -->', '');

const rowCount = (html: string) => (html.match(/changes-file-row/g) || []).length;

/** 取出所有统计徽标的**可见文本**（`<span class="changes-stat…">…</span>`）。
 *  断言必须落在徽标上，不能拿整页 HTML 去匹配 `-\d`——HTML 里的属性值也会命中，
 *  那样的断言测的是标记而不是"有没有编数字"。 */
const statTexts = (html: string): string[] =>
  [...html.matchAll(/<span class="changes-stat[^"]*"[^>]*>([\s\S]*?)<\/span>/g)].map((m) => m[1]);

/** 所有统计徽标都在说"不可得"（`—`），没有任何一个是数字。 */
function assertAllStatsUnavailable(html: string): void {
  const stats = statTexts(html);
  expect(stats.length).toBeGreaterThan(0);
  expect(stats.filter((s) => s !== '—')).toEqual([]);
}

describe('#189 AC1/AC2：文件列表', () => {
  it('一个文件一行（改两次也是一行），带路径与净统计', () => {
    const html = render([
      editTool('t1', 'src/a.ts', 'a\n', 'a\nb\n'),
      editTool('t2', 'src/b.ts', 'x\n', 'y\n'),
      editTool('t3', 'src/a.ts', 'a\nb\n', 'a\nb\nc\n'),
    ]);
    expect(rowCount(html)).toBe(2);
    expect(html).toContain('src/a.ts');
    expect(html).toContain('src/b.ts');
    // a.ts：原文 a → 最终 a,b,c = +2；b.ts：替换一行 = +1 −1
    expect(html).toContain('+2');
    expect(html).toContain('+1');
    expect(html).toContain('-1');
  });

  it('净零变化如实显示 ±0（不是隐藏统计，也不是编成 +1 −1）', () => {
    const html = render([
      editTool('t1', 'x.txt', 'a\n', 'a\nb\n'),
      editTool('t2', 'x.txt', 'a\nb\n', 'a\n'),
    ]);
    expect(html).toContain('±0');
  });

  it('默认选中第一个文件：右侧直接是它的 diff（不是空白右栏）', () => {
    const html = render([editTool('t1', 'src/a.ts', 'old\n', 'new\n')]);
    expect(html).toContain('diff-cols');
    expect(html).toContain('old');
    expect(html).toContain('new');
  });

  it('同一文件的多次改动按时间序都在右侧（各次一个 diff 块）', () => {
    const html = render([
      editTool('t1', 'src/a.ts', 'v1\n', 'v2\n'),
      editTool('t2', 'src/a.ts', 'v2\n', 'v3\n'),
    ]);
    expect((html.match(/diff-block/g) || []).length).toBe(2);
    expect(html).toContain('第 1 次改动');
    expect(html).toContain('第 2 次改动');
  });
});

describe('#189 AC5/AC6 + 不伪造：不可得与空态', () => {
  it('内容归档（>2000 字符）：统计显示"不可得"而不是假数字，占位由 DiffBlock 给', () => {
    const html = render([
      editTool('t1', 'big.txt', 'b', 'a', {
        diff: {
          before: 'b', after: 'a', truncated: false,
          archived: true, artifactId: '0123456789abcdef',
        },
      }),
    ]);
    expect(html).toContain('已归档');
    // DiffBlock 的归档占位（同一渲染器）在场
    expect(html).toContain('diff-archived');
    // 不得出现编出来的统计：**每一个**徽标（左清单行 + 右详情头）都必须说"不可得"，
    // 而不是各自编一个 +N/−M。按属性断言而不是按个数——多一个徽标不该让用例失效。
    assertAllStatsUnavailable(html);
  });

  it('内容被截断（>50KB）：同样说"不可得"并给出原因', () => {
    const html = render([
      editTool('t1', 'big.txt', 'a', 'b', { diff: { before: 'a', after: 'b', truncated: true } }),
    ]);
    expect(html).toContain('已截断');
    assertAllStatsUnavailable(html);
  });

  /* #186 AC4：marker 里的读回工具名要**原样透到界面**——默认部署（Local / MinIO）
     配的是 `read_artifact`，写死 `inspect_artifact` 会让用户照着复制出一句调不通的
     提示。这里同时覆盖「changedFiles → ChangesPanel → DiffBlock」这条透传链。 */
  it('归档占位回显 marker 里的工具名（read_artifact 不被偷换成 inspect_artifact）', () => {
    const html = render([
      editTool('t1', 'big.txt', 'b', 'a', {
        diff: {
          before: 'b', after: 'a', truncated: false, archived: true,
          artifactId: '0123456789abcdef', artifactTool: 'read_artifact',
        },
      }),
    ]);
    expect(html).toContain('read_artifact');
    expect(html).not.toContain('inspect_artifact');
  });

  /* #1311：空态是**统计口径声明**（"没有通过文件工具改动"），不是全称断言——
     bash 副作用不进清单是设计边界，文案不许说"未改动任何文件"。 */
  it('无改动：空态文案如实表达统计口径（不是空列表，也不是"加载中"）', () => {
    const html = render([]);
    expect(html).toContain('本次会话没有通过文件工具改动文件');
    expect(html).not.toContain('未改动任何文件');
    expect(rowCount(html)).toBe(0);
  });

  it('bash 不进清单（哪怕成功执行过）：清单仍为空，仍是口径声明（#1311 设计边界）', () => {
    const html = render([
      { tool_call_id: 'b', name: 'bash', args: { command: 'echo hi' }, status: 'success' } as ToolCall,
    ]);
    expect(rowCount(html)).toBe(0);
    expect(html).toContain('本次会话没有通过文件工具改动文件');
  });

  it('空态 + 存在执行过的 bash（status=success）：给弱提示（其文件副作用不在此清单内，#1311 AC3）', () => {
    const html = render([], { hasBashCall: true });
    expect(html).toContain('执行过的 bash 命令');
    // 口径中性：投影里 exit_code!=0 的 bash 也落 success，文案不许说"成功"
    expect(html).not.toContain('成功的 bash');
    expect(html).toContain('不在此清单');
  });

  it('空态 + 无执行过的 bash：不给弱提示（没有可提醒的事实）', () => {
    expect(render([])).not.toContain('bash 命令');
    // status='failed' 在生产里只对应 TOOL_EXECUTION_ERROR（沙箱拒绝/生成失败，
    // 命令未执行、无副作用）——不是"命令业务失败"（exit≠0 会落 success）。
    const noSuccess = render([
      { tool_call_id: 'b', name: 'bash', args: { command: 'ls' }, status: 'failed' } as ToolCall,
    ]);
    expect(noSuccess).not.toContain('bash 命令');
  });

  it('有文件 + 执行过 bash：不给弱提示（弱提示只属于空态版式，#1311 B/P4）', () => {
    const html = render([editTool('t1', 'src/a.ts', 'a\n', 'b\n')], { hasBashCall: true });
    expect(rowCount(html)).toBe(1);
    expect(html).not.toContain('执行过的 bash 命令');
  });

  it('弱提示与归因脚注语义不撞车：unattributed>0 时空态首句改口径，不再全称否定', () => {
    const html = render([editTool('t1', '   ', 'a', 'b')], { hasBashCall: false });
    expect(html).toContain('1 次改动无法归属到文件');
    // 首句与"另有 1 次改动无法归属到文件"同屏时不得自相矛盾（#1311 A/P2-2）
    expect(html).toContain('清单里没有可归属的文件改动');
    expect(html).not.toContain('本次会话没有通过文件工具改动文件');
    expect(html).not.toContain('bash 命令');
  });

  it('数据源标注（#1311 AC2）：空态与有文件两种版式下都在场，写明工具事件口径', () => {
    const empty = render([]);
    const withFiles = render([editTool('t1', 'src/a.ts', 'a\n', 'b\n')]);
    for (const html of [empty, withFiles]) {
      expect(html).toContain('数据来源：write/edit/apply_patch 工具事件');
      expect(html).toContain('shell 命令的文件副作用不在此清单内');
    }
  });

  it('有改动但归属不了文件（缺 path）：脚注如实说明数量，不静默丢', () => {
    const html = render([editTool('t1', '   ', 'a', 'b')]);
    expect(html).toContain('1 次改动无法归属到文件');
  });
});

describe('#189 AC4：只读面（不提供编辑入口）', () => {
  it('面板内不存在 input / textarea / contenteditable / 保存类按钮', () => {
    const html = render([
      editTool('t1', 'src/a.ts', 'old\n', 'new\n'),
      editTool('t2', 'src/a.ts', 'new\n', 'newer\n'),
    ]);
    expect(html).not.toContain('<input');
    expect(html).not.toContain('<textarea');
    expect(html).not.toContain('contenteditable');
    expect(html).not.toMatch(/保存|应用|撤销|编辑/);
  });
});

describe('task review summary', () => {
  it('shows a review entry after a terminal run', () => {
    const events: AgentEvent[] = [
      { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1' },
      { type: EventType.RUN_COMPLETED, data: {}, seq: 2, run_id: 'r1' },
    ];
    const html = renderToString(
      createElement(ChangesPanel, {
        ...changedFiles([]),
        events,
        eventsVersion: events.length,
        onOpenTaskReview: () => {},
      }),
    );

    expect(html).toContain('task-review-summary');
    expect(html).toContain('task-review-summary-link');
  });

  it('hides the summary while a run is open', () => {
    const events: AgentEvent[] = [
      { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1' },
    ];
    const html = renderToString(
      createElement(ChangesPanel, {
        ...changedFiles([]),
        events,
        eventsVersion: events.length,
        onOpenTaskReview: () => {},
      }),
    );

    expect(html).not.toContain('task-review-summary');
  });

  it('puts the whole summary in warning state when a run failed', () => {
    const events: AgentEvent[] = [
      { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1' },
      { type: EventType.RUN_FAILED, data: { reason: 'error' }, seq: 2, run_id: 'r1' },
    ];
    const html = renderToString(
      createElement(ChangesPanel, {
        ...changedFiles([]),
        events,
        eventsVersion: events.length,
        onOpenTaskReview: () => {},
      }),
    );

    expect(html).toContain('class="task-review-summary is-warning"');
  });

  it('does not scan run events while streaming', () => {
    const events = new Proxy([] as AgentEvent[], {
      get(target, property, receiver) {
        if (property === Symbol.iterator) throw new Error('events were scanned while streaming');
        return Reflect.get(target, property, receiver);
      },
    });

    expect(() => renderToString(
      createElement(ChangesPanel, {
        ...changedFiles([]),
        events,
        eventsVersion: 0,
        streaming: true,
      }),
    )).not.toThrow();
  });
});
