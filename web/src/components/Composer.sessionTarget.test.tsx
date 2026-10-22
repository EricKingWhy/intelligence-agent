// @vitest-environment jsdom
/** #1312：Composer 会话级目标状态行。
 *
 * AC1 的两态：
 *  - 选中会话（`sessionId` 真值）→「正在向会话 <8 位 id> 追问」+「改为新会话」；
 *  - 未选中（`sessionId` 空/缺省）→「将创建新会话」，无按钮。
 *
 * 用真挂载而不是 SSR 字符串：`改为新会话` 的价值在**点击后走到 onNewSession**
 * （App 侧接 `handleNew`），SSR 只能证明文案在，证明不了动作通。
 *
 * 第四条是 AC2 的机械下限：新状态行与 #1028 归属 chip 必须是**两个节点**、两块
 * 语义（会话身份 vs 项目归属），不能一边把另一边吃掉或并进同一个 role="note"。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Ownership } from '../lib/ownership';
import { Composer } from './Composer';

const SESSION_ID = 'abcdef12-3456-7890-abcd-ef1234567890';

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  // Composer 挂载后会 `loadImageLimits()`（Composer.tsx:419）——给 fetch 一个同步
  // 落定的响应，否则那次异步 setState 跑在 act 之外，测试会刷 act 警告。
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      max_image_bytes: 20 * 1024 * 1024,
      max_images_per_message: 20,
      max_message_image_bytes: 200 * 1024 * 1024,
      allowed_media_types: ['image/png', 'image/jpeg', 'image/webp', 'image/gif'],
    }),
  })));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function paint(props: {
  sessionId?: string | null;
  onNewSession?: () => void;
  ownership?: Ownership | null;
}): Promise<void> {
  await act(async () => {
    root.render(
      <Composer
        streaming={false}
        onSubmit={() => {}}
        onCancel={() => {}}
        onEnsureSession={async () => null}
        {...props}
      />,
    );
    // 冲掉 loadImageLimits 的 microtask，让它的 setLimits 落在 act 内。
    await Promise.resolve();
  });
}

const target = (): HTMLElement | null =>
  container.querySelector<HTMLElement>('[data-testid="composer-session-target"]');

describe('Composer 会话级目标状态行（#1312）', () => {
  it('选中会话：显示 8 位 id 与「改为新会话」，点击调用 onNewSession', async () => {
    const onNewSession = vi.fn();
    await paint({ sessionId: SESSION_ID, onNewSession });

    const statusLine = target();
    expect(statusLine).not.toBeNull();
    // live region 只圈文本：外层容器不带 role，「改为新会话」按钮留在它外面——
    // 否则状态播报会把一个可操作控件读进只读区域。
    expect(statusLine!.getAttribute('role')).toBeNull();
    const live = statusLine!.querySelector<HTMLElement>('[role="status"]');
    expect(live).not.toBeNull();
    expect(live!.textContent).toContain('正在向会话');

    // 只露前 8 位：precisely 断言 id 槽内容（旧断言 `not.toContain(完整 id)`
    // 在 id 槽为空时也恒真，证明不了截断）。
    const id = statusLine!.querySelector<HTMLElement>('.composer-session-target-id');
    expect(id).not.toBeNull();
    expect(id!.textContent).toBe('abcdef12');
    expect(id!.textContent).toMatch(/^[0-9a-f]{8}$/);
    expect(statusLine!.textContent).not.toContain(SESSION_ID);

    const button = statusLine!.querySelector<HTMLButtonElement>('button[aria-label="改为新会话"]');
    expect(button).not.toBeNull();
    expect(button!.classList.contains('composer-session-target-new-btn')).toBe(true);
    expect(button!.textContent).toBe('改为新会话');
    expect(live!.contains(button!)).toBe(false);

    act(() => button!.click());
    expect(onNewSession).toHaveBeenCalledTimes(1);
  });

  it('未选中会话：显示「将创建新会话」，且没有「改为新会话」按钮', async () => {
    await paint({ sessionId: null });

    const statusLine = target();
    expect(statusLine).not.toBeNull();
    expect(statusLine!.getAttribute('role')).toBeNull();
    expect(statusLine!.querySelector('[role="status"]')!.textContent).toBe('将创建新会话');
    expect(statusLine!.textContent).toBe('将创建新会话');
    expect(statusLine!.querySelector('button')).toBeNull();
  });

  it('空串 sessionId 视同未选中：不渲染「正在向会话  追问」', async () => {
    await paint({ sessionId: '' });

    const statusLine = target();
    expect(statusLine).not.toBeNull();
    expect(statusLine!.textContent).toBe('将创建新会话');
    expect(statusLine!.textContent).not.toContain('正在向会话');
    expect(statusLine!.querySelector('button')).toBeNull();
  });

  it('与 #1028 归属 chip 并存且互不吞并（会话身份 ≠ 项目归属）', async () => {
    await paint({
      sessionId: 'sess-1',
      onNewSession: () => {},
      ownership: {
        kind: 'project',
        projectTitle: '项目 alpha',
        cwd: 'D:/repos/alpha',
        label: '项目 alpha · D:/repos/alpha',
        title: '项目 alpha · D:/repos/alpha',
      },
    });

    const statusLine = target();
    const chip = container.querySelector<HTMLElement>('.composer-ownership');
    expect(statusLine).not.toBeNull();
    expect(chip).not.toBeNull();
    expect(statusLine!.contains(chip)).toBe(false);
    expect(chip!.contains(statusLine)).toBe(false);
    expect(chip!.getAttribute('role')).toBe('note');
    expect(statusLine!.textContent).toContain('正在向会话');
    expect(chip!.textContent).toContain('项目 alpha');
  });
});
