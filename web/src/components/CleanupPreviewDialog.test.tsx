// @vitest-environment jsdom
/** #368 [W-24] 清理预览浮层组件契约测试（Batch D）。
 *
 *  照 ConstraintResolutionDialog.test.tsx 的裸 react-dom/client + act 纪律（无 testing-library）。
 *  守三条：① 打开即 preview（loading → 内容）；② 确认按钮文案含勾选数 N；③ 回执诚实
 *  ——有 failed / not_deleted 时必须明示未删项，**严禁出现"全部清理"字样**。 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { CleanupPreviewDialog } from './CleanupPreviewDialog';
import type { CleanupPreview, CleanupResult } from '../lib/api';

function preview(over: Partial<CleanupPreview> = {}): CleanupPreview {
  return {
    snapshot_token: 'tok-1',
    affected: [
      { artifact_ref: 'art_aaaaaaaaaaaaaaaaaaaaaaaa', size: 1024, referenced_by: ['ev-1'] },
      { artifact_ref: 'art_b', size: 2 * 1024 * 1024, referenced_by: [] },
    ],
    evidence_invalidated: ['ev-1', 'ev-2'],
    reclaimable_bytes: 1024 + 2 * 1024 * 1024,
    blocked: [{ artifact_ref: 'art_c', reason: 'active_task' }],
    ...over,
  };
}

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

const text = () => document.body.textContent ?? '';
const buttonByText = (needle: string) =>
  [...document.querySelectorAll('button')].find((b) => (b.textContent ?? '').includes(needle)) as
    | HTMLButtonElement
    | undefined;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
});

describe('#368 CleanupPreviewDialog', () => {
  it('打开即拉预览：先 loading，解析后渲染 affected / blocked / 证据失效数量', async () => {
    let resolvePreview: (p: CleanupPreview) => void = () => {};
    const onPreview = vi.fn(
      () => new Promise<CleanupPreview>((resolve) => { resolvePreview = resolve; }),
    );
    render(
      <CleanupPreviewDialog
        target={{ sessionId: 's1' }}
        onOpenChange={() => {}}
        onPreview={onPreview}
        onConfirm={vi.fn()}
      />,
    );
    expect(onPreview).toHaveBeenCalledWith('s1');
    expect(text()).toContain('正在读取清理预览');

    await act(async () => {
      resolvePreview(preview());
      await Promise.resolve();
    });
    // affected：短 ref + 人性化大小 + referenced_by（#937 / M-22 起唯一实现是 1024 + IEC 标签）
    expect(text()).toContain('art_aaaaaaaaaaaaaaaa…');
    expect(text()).toContain('1 KiB');
    expect(text()).toContain('2 MiB');
    // blocked：中文解释
    expect(text()).toContain('art_c');
    expect(text()).toContain('有在途任务');
    // 证据失效警示
    expect(text()).toContain('将有 2 条证据失去可回读原件');
  });

  it('确认按钮文案含勾选数 N；取消勾选后 N 变化，0 个时禁用', async () => {
    const onPreview = vi.fn().mockResolvedValue(preview());
    render(
      <CleanupPreviewDialog
        target={{ sessionId: 's1' }}
        onOpenChange={() => {}}
        onPreview={onPreview}
        onConfirm={vi.fn()}
      />,
    );
    await act(async () => { await Promise.resolve(); });

    expect(buttonByText('确认清理 2 个原件')).toBeDefined();
    const boxes = [...document.querySelectorAll('input[type="checkbox"]')] as HTMLInputElement[];
    expect(boxes).toHaveLength(2);
    expect(boxes.every((b) => b.checked)).toBe(true);

    await act(async () => { boxes[0].click(); });
    expect(buttonByText('确认清理 1 个原件')).toBeDefined();

    await act(async () => { boxes[1].click(); });
    const disabled = buttonByText('确认清理 0 个原件');
    expect(disabled?.disabled).toBe(true);
  });

  it('确认 → onConfirm(sessionId, snapshot_token, 勾选 refs)，回执诚实展示（无"全部清理"）', async () => {
    const onPreview = vi.fn().mockResolvedValue(preview());
    const result: CleanupResult = {
      deleted: ['art_aaaaaaaaaaaaaaaaaaaaaaaa'],
      failed: ['art_b'],
      not_deleted: [{ artifact_ref: 'art_c', reason: 'referenced' }],
    };
    const onConfirm = vi.fn().mockResolvedValue(result);
    render(
      <CleanupPreviewDialog
        target={{ sessionId: 's1' }}
        onOpenChange={() => {}}
        onPreview={onPreview}
        onConfirm={onConfirm}
      />,
    );
    await act(async () => { await Promise.resolve(); });

    const confirm = buttonByText('确认清理 2 个原件')!;
    await act(async () => { confirm.click(); await Promise.resolve(); });

    expect(onConfirm).toHaveBeenCalledWith('s1', 'tok-1', [
      'art_aaaaaaaaaaaaaaaaaaaaaaaa',
      'art_b',
    ]);
    expect(text()).toContain('已删除 1 个原件');
    expect(text()).toContain('art_b');
    expect(text()).toContain('仍被事件引用');
    // 严禁谎报全部清理
    expect(text()).not.toContain('全部清理');
  });

  it('blocked/not_deleted 的 reason 全覆盖：evidence/not_found/invalid 均有中文解释', async () => {
    const onPreview = vi.fn().mockResolvedValue(
      preview({
        affected: [],
        blocked: [
          { artifact_ref: 'art_ev', reason: 'evidence' },
          { artifact_ref: 'art_nf', reason: 'not_found' },
          { artifact_ref: 'art_inv', reason: 'invalid' },
          { artifact_ref: 'art_unk', reason: 'mystery_reason' },
        ],
      }),
    );
    const result: CleanupResult = {
      deleted: [],
      failed: [],
      not_deleted: [
        { artifact_ref: 'art_nf2', reason: 'not_found' },
        { artifact_ref: 'art_inv2', reason: 'invalid' },
      ],
    };
    const onConfirm = vi.fn().mockResolvedValue(result);
    render(
      <CleanupPreviewDialog
        target={{ sessionId: 's1' }}
        onOpenChange={() => {}}
        onPreview={onPreview}
        onConfirm={onConfirm}
      />,
    );
    await act(async () => { await Promise.resolve(); });

    // preview.blocked：三种此前未映射的 reason 都有中文解释
    expect(text()).toContain('仍有新鲜证据引用');
    expect(text()).toContain('原件已不存在');
    expect(text()).toContain('引用格式非法');
    // 未知原因原样显示，不编
    expect(text()).toContain('mystery_reason');
  });

  it('确认失败（如 409 快照过期）→ 错误留在浮层，不伪装成功', async () => {
    const onPreview = vi.fn().mockResolvedValue(preview());
    const onConfirm = vi.fn().mockRejectedValue(new Error('清理预览已过期，请重新预览'));
    render(
      <CleanupPreviewDialog
        target={{ sessionId: 's1' }}
        onOpenChange={() => {}}
        onPreview={onPreview}
        onConfirm={onConfirm}
      />,
    );
    await act(async () => { await Promise.resolve(); });
    await act(async () => { buttonByText('确认清理 2 个原件')!.click(); await Promise.resolve(); });

    expect(text()).toContain('清理预览已过期，请重新预览');
    expect(text()).not.toContain('已删除');
  });

  it('取消勾选 → debounce 后带勾选集合重取 preview，确认用最新 token（方案 a）', async () => {
    const onPreview = vi
      .fn()
      .mockResolvedValueOnce(preview()) // 初次：全集 token tok-1
      .mockResolvedValue(preview({ snapshot_token: 'tok-2' })); // 重取：勾选子集 token
    const onConfirm = vi
      .fn()
      .mockResolvedValue({ deleted: [], failed: [], not_deleted: [] });
    render(
      <CleanupPreviewDialog
        target={{ sessionId: 's1' }}
        onOpenChange={() => {}}
        onPreview={onPreview}
        onConfirm={onConfirm}
      />,
    );
    await act(async () => { await Promise.resolve(); });
    expect(onPreview).toHaveBeenCalledTimes(1);

    const boxes = [...document.querySelectorAll('input[type="checkbox"]')] as HTMLInputElement[];
    await act(async () => { boxes[0].click(); });
    // debounce 未到点 → 尚未重取
    expect(onPreview).toHaveBeenCalledTimes(1);

    await act(async () => {
      await new Promise((r) => setTimeout(r, 350));
    });
    expect(onPreview).toHaveBeenCalledTimes(2);
    // 第二次带当前勾选集合（取消 art_aaaa… 后只剩 art_b）
    expect(onPreview).toHaveBeenLastCalledWith('s1', ['art_b']);

    // 确认必须用重取后的最新 token + 当前勾选
    await act(async () => {
      buttonByText('确认清理 1 个原件')!.click();
      await Promise.resolve();
    });
    expect(onConfirm).toHaveBeenCalledWith('s1', 'tok-2', ['art_b']);
  });
});
