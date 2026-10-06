/**
 * 主题：强调色 Pale Blossom #f1b3ca（#733 裁决，One Voice Rule）只出现在 accent 位
 * （标题、选中、焦点边框、badge），其余走中性灰与语义色。
 *
 * glyph 严格用 GBK 安全集（● ○ └ ─ │ ┌ ┐ ┘ ├ ┤）；braille spinner 与票面硬禁用的
 * 几个箭头勾叉字符在 Windows 中文终端会炸或宽度错乱，本包源码一律不写
 * （test/glyphs.test.ts 守门，禁用字符连注释里都不能出现）。
 */
import {
  backgroundAnsi,
  foregroundAnsi,
  parseColor,
  type Color,
  type TerminalColorMode,
} from "@earendil-works/pi-tui";

export { parseColor };

export const ACCENT = "#f1b3ca";
export const ACCENT_COLOR: Color = parseColor(ACCENT);

/** 中性与语义色（oklch/rgb 字面量交给 parseColor，终端色深自适应）。 */
export const COLORS = {
  muted: parseColor("#9aa0a8"),
  dim: parseColor("#7c828c"),
  text: parseColor("#d7dce3"),
  success: parseColor("#7fbf8e"),
  error: parseColor("#e08a8a"),
  warning: parseColor("#d9b06c"),
} as const;

/** GBK 安全 glyph 白名单（与 test/glyphs.test.ts 的票面白名单一致）。 */
export const GLYPHS = {
  dot: "●",
  circle: "○",
  branch: "└",
  hline: "─",
  vline: "│",
  topLeft: "┌",
  topRight: "┐",
  bottomRight: "┘",
  teeRight: "├",
  teeLeft: "┤",
} as const;

/** 工具卡状态底色 tint（Pi tool-execution 套路：状态靠底色传达，不靠图标）。 */
export const CARD_TINTS = {
  running: "rgb(38, 40, 46)",
  success: "rgb(30, 40, 33)",
  error: "rgb(46, 30, 33)",
} as const;

/** 主题函数集：组件需要 (text) => string 形态的颜色钩子。 */
export interface IaTheme {
  mode: TerminalColorMode;
  accent(text: string): string;
  muted(text: string): string;
  dim(text: string): string;
  text(text: string): string;
  success(text: string): string;
  error(text: string): string;
  warning(text: string): string;
}

export function createTheme(mode: TerminalColorMode): IaTheme {
  const fg = (color: Color) => (text: string) =>
    foregroundAnsi(color, mode) + text + "\x1b[39m";
  return {
    mode,
    accent: fg(ACCENT_COLOR),
    muted: fg(COLORS.muted),
    dim: fg(COLORS.dim),
    text: fg(COLORS.text),
    success: fg(COLORS.success),
    error: fg(COLORS.error),
    warning: fg(COLORS.warning),
  };
}

/** 底色染色函数（Box bgFn 用；保持文本字符不动，只包 ANSI bg）。 */
export function tintFn(color: Color, mode: TerminalColorMode): (text: string) => string {
  const bg = backgroundAnsi(color, mode);
  return (text: string) => bg + text + "\x1b[49m";
}

/** Loader 帧策略：Windows 用 ● ○ 脉冲（GBK 安全）；其余交给 Pi Loader 默认。 */
export function loaderFrames(platform: string): string[] | undefined {
  return platform === "win32" ? [GLYPHS.dot, GLYPHS.circle] : undefined;
}
