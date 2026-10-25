// @vitest-environment jsdom
/** #1509/#1520：错误横幅关闭后的焦点归宿链（纯函数级回归锁）。
 *
 * 三级链（票面 #1520 AC2 的判定顺序）：
 *  1. 触发该错误的控件——仍在 DOM、未禁用且可聚焦时优先；
 *  2. 横幅在 DOM 中的**下一个**可聚焦元素（跳过横幅自身子树，含其关闭按钮）；
 *  3. 都没有 → 横幅容器本身（`tabindex="-1"`，仅承接程序焦点）。
 *
 * App 集成只锁第 1 级与「不得落回 body」；三级判定与边界（禁用/离场/子树跳过）
 * 在这里精确锁。
 */
import { afterEach, describe, expect, it } from 'vitest';
import { resolveDismissFocusTarget } from './errorBannerFocus';

function div(id: string, attrs: Record<string, string> = {}): HTMLElement {
  const el = document.createElement('div');
  el.id = id;
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function button(id: string, attrs: Record<string, string> = {}): HTMLButtonElement {
  const el = document.createElement('button');
  el.id = id;
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

/** 每个用例自建小 DOM：region（常驻槽位）里挂 banner，关闭按钮在 banner 子树内，
 *  前后放干扰元素。 */
function fixture(): { banner: HTMLElement; region: HTMLElement; close: HTMLButtonElement; after: HTMLButtonElement } {
  document.body.innerHTML = '';
  const before = button('before');
  const region = div('region', { tabindex: '-1' });
  const banner = div('banner');
  const close = button('close');
  banner.appendChild(close);
  region.appendChild(banner);
  const after = button('after');
  document.body.append(before, region, after);
  return { banner, region, close, after };
}

afterEach(() => {
  document.body.innerHTML = '';
});

describe('resolveDismissFocusTarget — 三级焦点归宿链', () => {
  it('第 1 级：触发控件仍在场且可聚焦 → 回到它', () => {
    const { banner } = fixture();
    const trigger = button('trigger');
    document.body.appendChild(trigger);
    trigger.focus();
    expect(resolveDismissFocusTarget(banner, banner.parentElement, trigger)).toBe(trigger);
  });

  it('第 1 级降级：触发控件已离场（isConnected=false）→ 不再指它', () => {
    const { banner, after } = fixture();
    const trigger = button('trigger');
    document.body.appendChild(trigger);
    trigger.remove();
    expect(resolveDismissFocusTarget(banner, banner.parentElement, trigger)).not.toBe(trigger);
    expect(resolveDismissFocusTarget(banner, banner.parentElement, trigger)).toBe(after);
  });

  it('第 1 级降级：触发控件已禁用 → 落到横幅后的下一个可聚焦元素', () => {
    const { banner, after } = fixture();
    const trigger = button('trigger', { disabled: '' });
    document.body.appendChild(trigger);
    expect(resolveDismissFocusTarget(banner, banner.parentElement, trigger)).toBe(after);
  });

  it('第 2 级：取横幅**之后**的可聚焦元素，跳过横幅子树（含关闭按钮）与横幅之前的元素', () => {
    const { banner, after } = fixture();
    expect(resolveDismissFocusTarget(banner, banner.parentElement, null)).toBe(after);
  });

  it('第 3 级：横幅之后没有任何可聚焦元素 → 常驻槽位容器（tabindex=-1，不随横幅卸载）', () => {
    document.body.innerHTML = '';
    const region = div('region', { tabindex: '-1' });
    const banner = div('banner');
    const close = button('close');
    banner.appendChild(close);
    const before = button('before');
    region.appendChild(banner);
    document.body.append(before, region);
    expect(resolveDismissFocusTarget(banner, region, null)).toBe(region);
  });

  it('[tabindex="-1"] 元素不算「下一个可聚焦元素」，但 [tabindex="0"] 算', () => {
    document.body.innerHTML = '';
    const region = div('region', { tabindex: '-1' });
    const banner = div('banner');
    banner.appendChild(button('close'));
    const rove = div('rove', { tabindex: '-1' });
    const tabstop = div('tabstop', { tabindex: '0' });
    region.appendChild(banner);
    document.body.append(region, rove, tabstop);
    expect(resolveDismissFocusTarget(banner, region, null)).toBe(tabstop);
  });
});
