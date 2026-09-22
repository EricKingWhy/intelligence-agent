// @vitest-environment jsdom
/** 真机审计 A-06 的回归锁：示例任务 chip 注入后，焦点必须落进 composer 输入框。
 *
 *  为什么要 jsdom：判据是 `document.activeElement`——SSR（renderToString）没有焦点概念，
 *  既有 `Composer.test.tsx` 是 SSR 车道，看不到这条（同 `StepDetail.render.test.tsx` 的理由）。
 *
 *  缺陷形态（改造前）：chip 点完焦点留在 chip 按钮上，键盘用户接着按 Enter 是**再点一次
 *  chip**（等于没反应），与"注入即可编辑/发送"的意图相反。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Composer } from './Composer';

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const paint = (presetTask: { text: string; id: number } | null) =>
  act(() => {
    root.render(createElement(Composer, {
      streaming: false,
      onSubmit: () => {},
      onCancel: () => {},
      presetTask,
    }));
  });

describe('Composer：示例任务注入（A-06）', () => {
  it('注入后输入框拿到焦点且文本已就位（键盘接着按 Enter 就是发送）', () => {
    paint(null);
    const ta = container.querySelector<HTMLTextAreaElement>('#composer-input');
    expect(ta).not.toBeNull();
    // 先模拟"焦点在别处的按钮上"（chip 点击后的真实起点）
    const chip = document.createElement('button');
    document.body.appendChild(chip);
    chip.focus();
    expect(document.activeElement).toBe(chip);

    paint({ text: '写一个 FizzBuzz 脚本并运行验证', id: 1 });

    expect(container.querySelector<HTMLTextAreaElement>('#composer-input')?.value)
      .toBe('写一个 FizzBuzz 脚本并运行验证');
    expect(document.activeElement).toBe(container.querySelector('#composer-input'));
    chip.remove();
  });

  it('presetTask 为 null 时不抢焦点（不能把焦点从用户手里拿走）', () => {
    const outside = document.createElement('input');
    document.body.appendChild(outside);
    outside.focus();

    paint(null);

    expect(document.activeElement).toBe(outside);
    outside.remove();
  });
});
