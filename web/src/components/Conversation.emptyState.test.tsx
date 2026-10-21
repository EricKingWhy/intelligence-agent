// @vitest-environment jsdom
/** #1401 空状态契约（点击路径要真跑，不用 SSR 绿代替）。
 *
 *  为什么单开文件 + jsdom：`Conversation.test.tsx` 是 SSR 契约测试，`renderToStaticMarkup`
 *  会丢掉事件处理器 ⇒ 在那条车道里断言不出「点下去调的是哪个回调」。这里只锁两件事：
 *  ① 空态本体在、且不再渲染 #1370 的白底 CTA（本屏只留 composer 一个主入口）；
 *  ② 三个示例 chip 一个不少，点它们仍走 `onPresetTask`（降级为文字按钮，行为不变）。
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

describe('#1401 空状态', () => {
  it('渲染空态本体，且不再渲染重复主 CTA', () => {
    render(<Conversation conversation={null} loadingHistory={false} density="balanced" />);
    expect(container!.querySelector('.empty-state')).not.toBeNull();
    expect(container!.querySelector('.empty-hero')?.textContent).toBe('暂无对话');
    expect(container!.querySelector('.empty-cta')).toBeNull();
  });

  it('三个示例 chip 仍全部保留，点击仍走 onPresetTask', () => {
    const onPresetTask = vi.fn();
    render(
      <Conversation
        conversation={null}
        loadingHistory={false}
        density="balanced"
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
