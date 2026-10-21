/**
 * Theme — 初始化与持久化（冻结决策：主题切换持久化）。
 * CSS 侧 :root 默认暗色、[data-theme='light'] 覆盖（单一 light 来源，无 @media 重复）。
 * 必须在 React 挂载前执行（main.tsx 顶部调用），避免首帧闪错主题。
 */

export type Theme = 'dark' | 'light';

const STORAGE_KEY = 'ahi.theme';

function resolveInitial(): Theme {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved === 'dark' || saved === 'light') return saved;
  // 未持久化过：跟随系统偏好
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

export function initTheme(): Theme {
  const theme = resolveInitial();
  document.documentElement.setAttribute('data-theme', theme);
  return theme;
}

/** #1371：主题切换的过渡窗口属性。
 *
 *  为什么不是常驻 transition：canvas 之外的可见面（对话区卡片、消息气泡、工作
 *  面板……）逐个写 transition 会漏元素，而 `* { transition }` 常驻又会让 hover /
 *  拖拽带 200ms 迟滞。折中 = 只在切换瞬间挂 `data-theme-switch`，过渡时长
 *  （`--dur-theme`）过后摘掉：CSS 侧 `:root[data-theme-switch] *` 才声明过渡。属性与
 *  `data-theme` 在同一次样式重算前写入 ⇒ 过渡按 CSS Transitions 规范（取
 *  after-change 的 transition-property）照常启动。 */
const SWITCH_ATTR = 'data-theme-switch';
/** 略大于 `--dur-theme`（200ms）：留一帧调度余量，避免最后一帧被提前摘掉。 */
const SWITCH_WINDOW_MS = 240;
/** 用 `undefined` 而不是 `null`：DOM 的 `clearTimeout(id?: number)` 不收 `null`，
 *  而 clear 对 undefined 本就是 no-op（无需再套 if）。 */
let switchTimer: ReturnType<typeof setTimeout> | undefined;

function openSwitchWindow(): void {
  const root = document.documentElement;
  root.setAttribute(SWITCH_ATTR, '');
  clearTimeout(switchTimer);
  switchTimer = setTimeout(() => {
    root.removeAttribute(SWITCH_ATTR);
    switchTimer = undefined;
  }, SWITCH_WINDOW_MS);
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  // 同主题重复点击（或首帧设置）不需要过渡窗口——没有颜色变化，白挂 240ms
  // 的全站 transition 只会给同时发生的 hover / 拖拽加迟滞。
  if (root.getAttribute('data-theme') !== theme) openSwitchWindow();
  root.setAttribute('data-theme', theme);
  localStorage.setItem(STORAGE_KEY, theme);
}
