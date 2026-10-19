// @vitest-environment jsdom
/** #1370 P0-7 空状态主 CTA 的行为契约（点击路径要真跑，不用 SSR 绿代替）。
 *
 *  为什么单开文件 + jsdom：`Conversation.test.tsx` 是 SSR 契约测试，`renderToStaticMarkup`
 *  会丢掉事件处理器 ⇒ 在那条车道里断言不出「点下去调的是哪个回调」。这里只锁三件事：
 *  ① CTA 在（文案 + 可点）；② 点击落到注入的回调上（不造第二套新建入口）；③ 红线——
 *  三个示例 chip 一个不少，点它们仍走 `onPresetTask`。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Conversation } from './Conversation';

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

beforeEach(() => {
  // React 19 的 `act` 开关不在 TS 的 globalThis 声明里；命名断言集中在一处。
  const reactActEnv = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
  reactActEnv.IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
});

describe('#1370 空状态主 CTA', () => {
  it('注入 onNewTask 时渲染「开始新任务」，点击走到该回调（复用既有新建入口）', () => {
    const onNewTask = vi.fn();
    render(<Conversation conversation={null} loadingHistory={false} density="balanced" onNewTask={onNewTask} />);

    const cta = container!.querySelector<HTMLButtonElement>('button.empty-cta');
    expect(cta?.textContent).toBe('开始新任务');
    if (!cta) throw new Error('empty-cta 未渲染');
    act(() => cta.click());
    expect(onNewTask).toHaveBeenCalledTimes(1);
  });

  it('未注入 onNewTask 时不渲染 CTA（不出现点了没反应的按钮）', () => {
    render(<Conversation conversation={null} loadingHistory={false} density="balanced" />);
    expect(container!.querySelector('.empty-cta')).toBeNull();
    // 空态本体仍在：CTA 是可选项，不是空态的前提。
    expect(container!.querySelector('.empty-hero')?.textContent).toBe('暂无对话');
  });

  it('红线：三个示例 chip 仍全部保留，点击仍走 onPresetTask', () => {
    const onPresetTask = vi.fn();
    render(
      <Conversation
        conversation={null}
        loadingHistory={false}
        density="balanced"
        onNewTask={() => {}}
        onPresetTask={onPresetTask}
      />,
    );

    const chips = container!.querySelectorAll<HTMLElement>('.empty-examples .example-chip');
    expect(chips).toHaveLength(3);
    act(() => chips[1].click());
    expect(onPresetTask).toHaveBeenCalledTimes(1);
    expect(onPresetTask.mock.calls[0][0]).toBe(chips[1].textContent);
  });
});
