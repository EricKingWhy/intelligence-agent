/**
 * WSL 判定（零依赖纯函数）。
 *
 * 来源：Pi `packages/coding-agent/src/utils/wsl.ts:1-15` @ commit
 * `28dcce2ba45ce4a9efeb0f5b686f0be830fd89b9`（MIT，许可全文见
 * `tui/THIRD_PARTY_NOTICES.md`）。判定 **COPY**（行为逐行保留；仅把 `env` 参数
 * 的缺省写法显式化，便于测试注入）。
 *
 * 用途（AC1）：WSL 下 `Alt+V` / `Ctrl+V` 要经 `powershell.exe` 取 Windows 侧剪贴板
 * （WSL 的 Wayland/X11 剪贴板收不到 Win+Shift+S 的截图）。
 */
import { readFileSync } from "node:fs";

/** Windows Subsystem for Linux：Windows 可执行文件经 interop 可达。 */
export function isWSL(env: NodeJS.ProcessEnv = process.env): boolean {
  if (env["WSL_DISTRO_NAME"] || env["WSLENV"]) {
    return true;
  }

  try {
    const release = readFileSync("/proc/version", "utf-8");
    return /microsoft|wsl/i.test(release);
  } catch {
    return false;
  }
}
