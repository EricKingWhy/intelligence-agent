import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { applyTheme } from './theme';

/** 最小宿主 stub：本模块只触碰 <html> 的两个属性、localStorage 与 matchMedia。 */
function installDomStub(): { attrs: Record<string, string>; store: Record<string, string> } {
  const attrs: Record<string, string> = {};
  const store: Record<string, string> = {};
  vi.stubGlobal('document', {
    documentElement: {
      getAttribute: (k: string) => attrs[k] ?? null,
      setAttribute: (k: string, v: string) => void (attrs[k] = v),
      removeAttribute: (k: string) => void delete attrs[k],
    },
  });
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store[k] ?? null,
    setItem: (k: string, v: string) => void (store[k] = v),
  });
  vi.stubGlobal('window', {
    matchMedia: () => ({ matches: false }),
  });
  return { attrs, store };
}

describe('applyTheme — 主题切换的过渡窗口（#1371 P1-1）', () => {
  let attrs: Record<string, string>;
  let store: Record<string, string>;

  beforeEach(() => {
    vi.useFakeTimers();
    ({ attrs, store } = installDomStub());
    attrs['data-theme'] = 'dark';
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('切换主题：同一次样式重算前挂上过渡窗口，240ms 后摘掉', () => {
    applyTheme('light');
    expect(attrs['data-theme']).toBe('light');
    // 窗口属性必须与 data-theme 在同一拍写入——CSS Transitions 取 after-change
    // 的 transition-property，晚一拍就只换色不插值。
    expect('data-theme-switch' in attrs).toBe(true);
    expect(store['ahi.theme']).toBe('light');

    vi.advanceTimersByTime(239);
    expect('data-theme-switch' in attrs).toBe(true);
    vi.advanceTimersByTime(1);
    expect('data-theme-switch' in attrs).toBe(false);
  });

  it('同主题重复设置不开窗口：没有颜色变化就不给 hover / 拖拽加迟滞', () => {
    applyTheme('dark');
    expect(attrs['data-theme']).toBe('dark');
    expect('data-theme-switch' in attrs).toBe(false);
    // 持久化照旧（调用方可能用它做首次落盘）。
    expect(store['ahi.theme']).toBe('dark');
  });

  it('窗口内连点：计时器重置，最后一帧不会被提前摘掉', () => {
    applyTheme('light');
    vi.advanceTimersByTime(200);
    applyTheme('dark');
    expect('data-theme-switch' in attrs).toBe(true);
    vi.advanceTimersByTime(200);
    expect('data-theme-switch' in attrs).toBe(true);
    vi.advanceTimersByTime(40);
    expect('data-theme-switch' in attrs).toBe(false);
  });
});
