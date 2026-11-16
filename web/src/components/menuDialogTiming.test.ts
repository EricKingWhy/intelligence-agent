// @vitest-environment jsdom
/** #1694 helper 自检：四步时序各走一遍，以及「没有菜单触发时一律放行」。 */
import { afterEach, describe, expect, it } from 'vitest';
import {
  deferMenuInitialFocus,
  focusFirstIn,
  openMenuDialog,
  takeMenuCloseAutoFocus,
} from './menuDialogTiming';

const cancelable = () => ({ defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }) as unknown as Event & { defaultPrevented: boolean };

afterEach(() => {
  document.body.style.pointerEvents = '';
});

describe('menuDialogTiming', () => {
  it('菜单触发的弹窗：推迟初焦 → 关闭时挡掉归还并落焦', async () => {
    const opened: string[] = [];
    const focused: string[] = [];
    document.body.style.pointerEvents = 'none';

    openMenuDialog(() => opened.push('dialog'));
    /* 菜单留下的 body 锁必须在弹窗 mount 前摘掉，否则弹窗会把它当"原始值"写回。 */
    expect(document.body.style.pointerEvents).toBe('');
    expect(opened).toEqual(['dialog']);

    const mountEvent = cancelable();
    expect(deferMenuInitialFocus(mountEvent, () => focused.push('first'))).toBe(true);
    expect(mountEvent.defaultPrevented).toBe(true);

    const closeEvent = cancelable();
    expect(takeMenuCloseAutoFocus(closeEvent)).toBe(true);
    expect(closeEvent.defaultPrevented).toBe(true);

    await new Promise((r) => setTimeout(r, 5));
    expect(focused).toEqual(['first']);

    /* 标志用完即清：下一次关闭不应再挡。 */
    expect(takeMenuCloseAutoFocus(cancelable())).toBe(false);
  });

  it('不由菜单触发的弹窗：两个入口都放行，不劫持焦点', () => {
    const mountEvent = cancelable();
    expect(deferMenuInitialFocus(mountEvent, () => {})).toBe(false);
    expect(mountEvent.defaultPrevented).toBe(false);
    expect(takeMenuCloseAutoFocus(cancelable())).toBe(false);
  });

  it('focusFirstIn 跳过 tabindex=-1，落到首个真正可聚焦的元素', () => {
    const root = document.createElement('div');
    root.innerHTML = '<button tabindex="-1">关闭</button><input id="a" /><button id="b">取消</button>';
    document.body.append(root);
    focusFirstIn(root);
    expect(document.activeElement?.id).toBe('a');
    root.remove();
  });
});
