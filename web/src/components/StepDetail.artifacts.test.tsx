// @vitest-environment jsdom
/** #1419 A：产出面 = 段头 + 2 列卡片网格 + 选中项预览面板。
 *
 *  只断言可观察行为与真实数据：卡上的名/大小/seq/工具/时间来自投影字段，
 *  缺元数据说「未知」（不编 `0 B`），预览面板默认看最新一份、点卡片换选中项。
 *  内容取数（`ArtifactViewer`）在 jsdom 里停在 loading 态——那一层的三态由
 *  `ArtifactViewer.test.tsx` 的 SSR 用例覆盖，这里只钉"选中项传对了 artifact"。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { formatClock } from '../lib/timelineView';
import type { ToolCall } from '../types';
import { ArtifactsTab } from './StepDetail';

/* 取数停在 loading：本文件只钉"选中项传给谁"，内容三态由 `ArtifactViewer.test.tsx`
 * 的 SSR 用例覆盖（那里不需要真取数）。 */
vi.mock('../lib/api', () => ({
  // 永不落定：本文件只考"选中项传给谁"，内容三态由 `ArtifactViewer.test.tsx` 覆盖。
  getArtifactContent: vi.fn(() => new Promise<never>(() => {})),
  ArtifactContentError: class extends Error {},
}));

let container: HTMLDivElement | null = null;
let root: Root | null = null;

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  container = null;
  root = null;
});

const noop = () => {};

const art = (
  id: string,
  over: Partial<ToolCall['artifact']> = {},
  toolOver: Partial<ToolCall> = {},
): ToolCall => ({
  tool_call_id: `tc-${id}`,
  name: 'write',
  args: { path: `out/${id}.md` },
  status: 'success',
  started_at: '2026-10-20T10:23:49.000Z',
  completed_at: '2026-10-20T10:23:57.000Z',
  raw_call: { seq: 13 },
  artifact: {
    artifact_id: `${id}0123456789abcdef0123456789abcdef`,
    size: 6350,
    mime_type: 'text/markdown',
    source_tool: 'write',
    ...over,
  },
  ...toolOver,
});

const render = (tools: ToolCall[]) =>
  act(() => root!.render(<ArtifactsTab tools={tools} sessionId="s1" onFocusTool={noop} />));
const cards = () => Array.from(container!.querySelectorAll<HTMLElement>('.art-card'));

describe('#1419 Artifacts A-1 卡片网格', () => {
  it('段头：标签 + `N 个` + artifact 胶囊', () => {
    render([art('a'), art('b')]);
    const head = container!.querySelector('.pane-head')!;
    expect(head.textContent).toContain('产出文件');
    expect(head.textContent).toContain('2 个');
    expect(head.textContent).toContain('artifact');
  });

  it('卡片带真实元信息：路径名 / 大小 / seq / 产生它的工具 / 时刻', () => {
    render([art('a')]);
    const c = cards()[0];
    expect(c.querySelector('.art-name')?.textContent).toBe('out/a.md');
    const meta = c.querySelector('.art-meta')!.textContent!;
    // 大小走既有 `formatBytes`（6 KiB 档），不另造一套单位
    expect(meta).toContain('6 KiB');
    expect(meta).toContain('seq 13');
    expect(meta).toContain('write');
    const expectedClock = formatClock('2026-10-20T10:23:57.000Z');
    expect(expectedClock).not.toBeNull();
    expect(meta).toContain(expectedClock!.slice(0, 5));
    expect(c.querySelector('.art-badge')?.textContent).toContain('已生成');
  });

  it('缺元数据说「未知」，不编 0 B；没有路径时用工具名 + 编号前缀，不编文件名', () => {
    render([
      art('x', { size: null, mime_type: null, source_tool: null }, { args: {}, name: 'bash' }),
    ]);
    const c = cards()[0];
    expect(c.querySelector('.art-meta')?.textContent).toContain('未知');
    expect(c.querySelector('.art-meta')?.textContent).not.toContain('0 B');
    expect(c.querySelector('.art-name')?.textContent).toBe('bash · x0123456');
  });

  it('2 列网格：容器是 `.art-grid`（效果图布局）', () => {
    render([art('a'), art('b')]);
    expect(container!.querySelector('.art-grid')?.children).toHaveLength(2);
  });
});

describe('#1419 Artifacts A-2 预览面板', () => {
  it('默认预览最新一份，点另一张卡切换', () => {
    render([art('a'), art('b')]);
    const preview = () => container!.querySelector('.art-preview')!;
    expect(preview().querySelector('.pv-name')?.textContent).toBe('out/b.md');
    expect(cards()[1].getAttribute('aria-pressed')).toBe('true');
    act(() => cards()[0].click());
    expect(preview().querySelector('.pv-name')?.textContent).toBe('out/a.md');
    expect(cards()[0].getAttribute('aria-pressed')).toBe('true');
    expect(cards()[1].getAttribute('aria-pressed')).toBe('false');
  });

  it('预览头给出真实副行（工具 · seq · mime），尾给出 artifact 编号', () => {
    render([art('a')]);
    const p = container!.querySelector('.art-preview')!;
    const sub = p.querySelector('.pv-sub')!.textContent!;
    expect(sub).toContain('write');
    expect(sub).toContain('seq 13');
    expect(sub).toContain('text/markdown');
    expect(p.querySelector('.pv-foot code')?.textContent).toBe('a0123456789abcde');
  });

  it('预览里没有第二套开合钮（内容由选中项决定，`bare` 模式）', () => {
    render([art('a')]);
    expect(container!.querySelector('.art-preview .artifact-toggle')).toBeNull();
    expect(container!.querySelector('.art-preview .artifact-viewer')).not.toBeNull();
  });

  it('「查看事件」把选中项交回面板（非只读场景）', () => {
    const seen: ToolCall[] = [];
    act(() =>
      root!.render(
        <ArtifactsTab tools={[art('a')]} sessionId="s1" onFocusTool={(t) => seen.push(t)} />,
      ),
    );
    const btn = container!.querySelector<HTMLElement>('.art-preview .ov-toggle')!;
    act(() => btn.click());
    expect(seen).toHaveLength(1);
    expect(seen[0].tool_call_id).toBe('tc-a');
  });

  it('只读场景（不给 onFocusTool）不画点不动的按钮', () => {
    act(() => root!.render(<ArtifactsTab tools={[art('a')]} sessionId="s1" />));
    expect(container!.querySelector('.art-preview .ov-toggle')).toBeNull();
  });
});
