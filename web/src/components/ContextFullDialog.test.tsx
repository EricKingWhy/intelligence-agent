// @vitest-environment jsdom
/** 上下文满了一句话弹窗（#1197 项 2）——压不住时**只说人话**。
 *
 * 票面（效果图 `docs/ux-mockups/2026-10-16-context-compact/mockup.html` 的
 * `#failed-state`）：水位到线先自动压；压不住（没东西可压）才弹**一句话**：
 *   「上下文已经满了」/「前面没有可压缩的内容，换个窗口更大的模型继续吧。」
 *   按钮：换模型 / 结束。
 * 设计说明 02：「失败时只说人话 —— 不再展示预算、ceiling、水位等内部术语，只给
 * 原因和下一步。」
 *
 * 挂载方式照抄 `ConstraintResolutionDialog.test.tsx`（真 Radix Dialog + createRoot）：
 * Radix 走 Portal，`renderToStaticMarkup` 看不到内容。
 *
 * 本文件钉两件事：
 *   1. **该有的在**：标题 + 一句话 + 两个按钮。
 *   2. **不该有的不在**：内部术语（预算到顶 / ceiling / max_context_tokens /
 *      水位 / 压缩版本 / 收口）一个都不许出现——那正是本项要拿掉的技术屏。
 */
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ContextFullDialog } from './ContextFullDialog';

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function render(ui: ReactElement): void {
  if (!container) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }
  act(() => root!.render(ui));
}

/** 挂载并返回弹窗文本（Radix Portal 落在 body 上，不在 container 里）。 */
function mount(overrides: Partial<Parameters<typeof ContextFullDialog>[0]> = {}): string {
  render(<ContextFullDialog open onEndRun={() => {}} onChangeModel={() => {}} {...overrides} />);
  return document.body.textContent ?? '';
}

function buttonByText(text: string): HTMLButtonElement {
  const found = Array.from(document.body.querySelectorAll('button')).find((b) =>
    (b.textContent ?? '').includes(text),
  );
  if (!found) throw new Error(`没有找到按钮：${text}`);
  return found as HTMLButtonElement;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  document.body.innerHTML = '';
  container = null;
  root = null;
});

describe('#1197 项 2 — ContextFullDialog（压不住时的一句话兜底）', () => {
  it('渲染票面那一句话与两个按钮', () => {
    const text = mount();
    expect(text).toContain('上下文已经满了');
    expect(text).toContain('前面没有可压缩的内容，换个窗口更大的模型继续吧。');
    expect(text).toContain('换模型');
    expect(text).toContain('结束');
  });

  it('#1325 S9：自动满窗与手动被拒同一语言（中文、同用「没有可压缩」措辞）', () => {
    const text = mount();
    // 手动路径的 409 `no_early_turns` 在 `useCompactSession` 映射为
    // 「没有可压缩的早期内容」；自动路径保持票面中文引导，两者共享同一措辞，
    // 不出现英文原文 / 机读 reason / 裸 session id。
    expect(text).toContain('没有可压缩');
    expect(text).not.toContain('no_early_turns');
    expect(text).not.toContain('session');
    expect(text).not.toMatch(/[A-Za-z]{3,}/);
  });

  it('不出现任何内部术语（设计说明 02：只说人话）', () => {
    const text = mount();
    for (const jargon of [
      '预算到顶',
      'ceiling',
      'max_context_tokens',
      '水位',
      'budget_exhausted',
      '预算版本',
      '收口',
      'local fuse',
      'trace',
    ]) {
      expect(text).not.toContain(jargon);
    }
  });

  it('是个可读的模态对话框（role=dialog + 标题/描述真的被关联上）', () => {
    mount();
    const dialog = document.body.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    // 本版 Radix 的模态语义是"焦点陷阱 + 背景 aria-hidden"，不写 aria-modal；
    // 只钉住它真给的东西：role=dialog + 两个关联 id 指向**真实存在**的节点。
    // （这条断言抓到过一次真 bug：手写 id 覆盖了 Radix 发给 Dialog.Title 的 id，
    //  aria-labelledby 因此指向不存在的节点。）
    const labelledBy = dialog!.getAttribute('aria-labelledby');
    const describedBy = dialog!.getAttribute('aria-describedby');
    expect(labelledBy).toBeTruthy();
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(labelledBy!)?.textContent).toContain('上下文已经满了');
    expect(document.getElementById(describedBy!)?.textContent).toContain('换个窗口更大的模型');
  });

  it('open=false 时不渲染（收起是真的收起）', () => {
    const text = mount({ open: false });
    expect(text).not.toContain('上下文已经满了');
  });

  it('换模型 / 结束 各自回调一次', () => {
    const onEndRun = vi.fn();
    const onChangeModel = vi.fn();
    mount({ onEndRun, onChangeModel });
    act(() => buttonByText('换模型').click());
    expect(onChangeModel).toHaveBeenCalledTimes(1);
    expect(onEndRun).not.toHaveBeenCalled();
    act(() => buttonByText('结束').click());
    expect(onEndRun).toHaveBeenCalledTimes(1);
    expect(onChangeModel).toHaveBeenCalledTimes(1);
  });
});
