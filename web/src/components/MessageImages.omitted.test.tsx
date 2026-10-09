// @vitest-environment jsdom
/** #825（MM-04）AC7 **后半**：「已附图但当前模型不支持视觉 ⇒ 标注『图已被省略』」。
 *
 *  为什么单独钉这一条：AC7 前半（入口禁用 + 原因）有 e2e 与单测；后半此前只有实现
 *  声明——把 `omitted` 写成恒 false、或把标注文案/`title` 改错，四条车道仍全绿
 *  （独立审查 F2）。这里的判据是"标注是否存在、文案与 `title` 是否与后端占位符逐字
 *  同源"，只能靠渲染断言。
 *
 *  jsdom + 真实客户端渲染：`MessageImages` 里有 `useState`（每张图自己的浮层开关），
 *  且断言的是条件渲染的兄弟节点，SSR 车道不覆盖交互形态（先例同
 *  `Composer.attachments.test.tsx`）。
 */

import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

import { IMAGE_OMITTED_PLACEHOLDER } from '../lib/attachments';
import type { ImageAttachmentRef } from '../lib/attachmentRefs';
import { MessageImages } from './MessageImages';

const REF: ImageAttachmentRef = {
  kind: 'image',
  attachment_id: 'sha256:aaa',
  media_type: 'image/png',
  bytes: 12,
  width: 8,
  height: 8,
  name: null,
};

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

function paint(images: readonly ImageAttachmentRef[], omitted: boolean): void {
  act(() => {
    root.render(createElement(MessageImages, { sessionId: 'session-1', images, omitted }));
  });
}

describe('#825 AC7：非视觉模型下的「图已被省略」标注', () => {
  it('omitted=true 时标注与图共存，文案与 title 与后端占位符同源', () => {
    paint([REF], true);
    // 图本身仍在（历史附图保留可见性），标注是**附加**事实而不是替换。
    expect(container.querySelector('.msg-image-img')?.getAttribute('src')).toContain(
      '/api/sessions/session-1/attachments/sha256%3Aaaa/content',
    );
    const omitted = container.querySelector('.msg-images-omitted');
    expect(omitted).not.toBeNull();
    expect(omitted?.textContent).toContain('图已被省略');
    // title 逐字用后端投影同一句占位符：两边文案漂移时用户会看到两种说法。
    expect(omitted?.getAttribute('title')).toBe(IMAGE_OMITTED_PLACEHOLDER);
  });

  it('omitted=false（支持视觉 / 后端沉默）时不得标注', () => {
    paint([REF], false);
    expect(container.querySelector('.msg-image-img')).not.toBeNull();
    expect(container.querySelector('.msg-images-omitted')).toBeNull();
  });

  it('没有图时整个元素不渲染（纯文本轮次的 DOM 与改动前一致）', () => {
    paint([], true);
    expect(container.querySelector('.msg-images')).toBeNull();
  });
});
