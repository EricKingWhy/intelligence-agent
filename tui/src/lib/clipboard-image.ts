/**
 * 剪贴板取图：平台阶梯 + WSL 经 `powershell.exe` 取 Windows 侧剪贴板。
 *
 * 来源：Pi `packages/coding-agent/src/utils/clipboard-image.ts:1-240` @ commit
 * `28dcce2ba45ce4a9efeb0f5b686f0be830fd89b9`（MIT，许可全文见
 * `tui/THIRD_PARTY_NOTICES.md`）。判定 **COPY（含三处已声明偏离）**：
 *
 * 1. **不引 Photon**（`loadPhoton()` + 非支持格式转 PNG）：本票红线是「不新增 TUI 库」，
 *    Photon 是上游的可选原生依赖。偏离后果：剪贴板给出本仓不收的格式（如 Windows DIB
 *    包成 BMP）时**返回 null**（明确"没有可用图片"），而不是静默转码；
 * 2. **支持的 media type 集从本仓服务端契约单点引入**（`image-paste.ts`），
 *    与 `POST /api/sessions/{id}/attachments` 的接纳集一致（上游那份是同一组值）；
 * 3. 子进程读取器 / 原生剪贴板 / 临时目录**可注入**--Linux 沙箱里没有 Windows 剪贴板，
 *    AC1 的 powershell 分支只能靠注入的假 `powershell.exe` 覆盖（本票登记的验证缺口）。
 *
 * 阶梯（与上游逐行一致）：
 * `TERMUX_VERSION` -> null；`linux` 且（Wayland 或 WSL）-> 先 `wl-paste`，失败再 `xclip`，
 * 都无图且是 WSL -> `powershell.exe`，再不行 -> pi-tui 原生剪贴板；非 linux -> 原生剪贴板。
 */
import { randomUUID } from "node:crypto";
import { readFileSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { getNativeClipboard, type NativeClipboard } from "@earendil-works/pi-tui";

import { runClipboardCommand, type ClipboardCommandOptions } from "./clipboard-command.ts";
import { SUPPORTED_IMAGE_MEDIA_TYPES } from "./image-paste.ts";
import { detectSupportedImageMimeType } from "./mime.ts";
import { isWSL } from "./wsl.ts";

export interface ClipboardImage {
  bytes: Uint8Array;
  mimeType: string;
}

/** 子进程读取器（默认 `runClipboardCommand`；测试注入假 `powershell.exe`）。 */
export type ClipboardImageRun = (
  command: string,
  args: readonly string[],
  options?: ClipboardCommandOptions,
) => Promise<Buffer | undefined>;

export interface ClipboardImageOptions {
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  run?: ClipboardImageRun;
  nativeClipboard?: () => NativeClipboard | undefined;
  /** powershell 回读用的临时目录（默认 `os.tmpdir()`；测试注入）。 */
  tmpDir?: string;
}

interface ClipboardDeps {
  run: ClipboardImageRun;
  nativeClipboard: () => NativeClipboard | undefined;
  tmpDir: string;
}

const DEFAULT_LIST_TIMEOUT_MS = 1000;
const DEFAULT_POWERSHELL_TIMEOUT_MS = 5000;

/** Wayland 会话判定（决定是否先试 `wl-paste`）。 */
export function isWaylandSession(env: NodeJS.ProcessEnv = process.env): boolean {
  return Boolean(env["WAYLAND_DISPLAY"]) || env["XDG_SESSION_TYPE"] === "wayland";
}

/**
 * 粘贴剪贴板图片的键位 id（pi-tui `matchesKey` 的键标识，如 `Key.alt("v")`）。
 */
export type ClipboardImageKey = "alt+v" | "ctrl+v";

/**
 * 粘贴剪贴板图片的键位（AC1）：Windows 用 `Alt+V`（`Ctrl+V` 常被终端截获成"粘贴文本"，
 * 到不了应用）；WSL **双绑**（`Ctrl+V` 与 `Alt+V` 都触发，取的是 Windows 侧剪贴板）。
 */
export function clipboardImageBindings(options?: {
  platform?: NodeJS.Platform;
  env?: NodeJS.ProcessEnv;
}): ClipboardImageKey[] {
  const platform = options?.platform ?? process.platform;
  const env = options?.env ?? process.env;
  if (platform === "win32") return ["alt+v"];
  if (platform === "linux" && isWSL(env)) return ["ctrl+v", "alt+v"];
  return ["alt+v"];
}

function baseMimeType(mimeType: string): string {
  return mimeType.split(";")[0]?.trim().toLowerCase() ?? mimeType.toLowerCase();
}

function isSupportedImageMimeType(mimeType: string): boolean {
  const base = baseMimeType(mimeType);
  return SUPPORTED_IMAGE_MEDIA_TYPES.some((t) => t === base);
}

/** 从候选类型里选首选（PNG > JPEG > WEBP > GIF，再退任何 `image/*`）。 */
function selectPreferredImageMimeType(mimeTypes: string[]): string | null {
  const normalized = mimeTypes
    .map((t) => t.trim())
    .filter(Boolean)
    .map((t) => ({ raw: t, base: baseMimeType(t) }));

  for (const preferred of SUPPORTED_IMAGE_MEDIA_TYPES) {
    const match = normalized.find((t) => t.base === preferred);
    if (match) {
      return match.raw;
    }
  }

  const anyImage = normalized.find((t) => t.base.startsWith("image/"));
  return anyImage?.raw ?? null;
}

function splitLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((t) => t.trim())
    .filter(Boolean);
}

// Undefined means the backend failed; null means it has no image. An empty
// Wayland clipboard must not fall through to stale X11 clipboard contents.
async function readClipboardImageViaWlPaste(
  deps: ClipboardDeps,
): Promise<ClipboardImage | null | undefined> {
  const list = await deps.run("wl-paste", ["--list-types"], { timeoutMs: DEFAULT_LIST_TIMEOUT_MS });
  if (list === undefined) return undefined;

  const selectedType = selectPreferredImageMimeType(splitLines(list.toString("utf-8")));
  if (!selectedType) {
    return null;
  }

  const data = await deps.run("wl-paste", ["--type", selectedType, "--no-newline"]);
  if (data === undefined) return undefined;
  if (data.length === 0) return null;

  return { bytes: data, mimeType: baseMimeType(selectedType) };
}

/**
 * WSL 上 Linux 剪贴板收不到 Windows 截图（Win+Shift+S），但 PowerShell 能直接读
 * Windows 剪贴板 -- 让它把图存成 PNG 再读回。
 */
async function readClipboardImageViaPowerShell(deps: ClipboardDeps): Promise<ClipboardImage | null> {
  const tmpFile = join(deps.tmpDir, `ia-tui-wsl-clip-${randomUUID()}.png`);

  try {
    const winPathResult = await deps.run("wslpath", ["-w", tmpFile], {
      timeoutMs: DEFAULT_LIST_TIMEOUT_MS,
    });
    if (winPathResult === undefined) {
      return null;
    }

    const winPath = winPathResult.toString("utf-8").trim();
    if (!winPath) {
      return null;
    }

    const psQuotedWinPath = winPath.replaceAll("'", "''");
    const psScript = [
      "Add-Type -AssemblyName System.Windows.Forms",
      "Add-Type -AssemblyName System.Drawing",
      `$path = '${psQuotedWinPath}'`,
      "$img = [System.Windows.Forms.Clipboard]::GetImage()",
      "if ($img) { $img.Save($path, [System.Drawing.Imaging.ImageFormat]::Png); Write-Output 'ok' } else { Write-Output 'empty' }",
    ].join("; ");

    const result = await deps.run("powershell.exe", ["-NoProfile", "-Command", psScript], {
      timeoutMs: DEFAULT_POWERSHELL_TIMEOUT_MS,
    });
    if (result === undefined) {
      return null;
    }

    if (result.toString("utf-8").trim() !== "ok") {
      return null;
    }

    const bytes = readFileSync(tmpFile);
    if (bytes.length === 0) {
      return null;
    }

    return { bytes: new Uint8Array(bytes), mimeType: "image/png" };
  } catch {
    return null;
  } finally {
    try {
      unlinkSync(tmpFile);
    } catch {
      // Ignore cleanup errors.
    }
  }
}

async function readClipboardImageViaXclip(
  deps: ClipboardDeps,
): Promise<ClipboardImage | null | undefined> {
  const targets = await deps.run("xclip", ["-selection", "clipboard", "-t", "TARGETS", "-o"], {
    timeoutMs: DEFAULT_LIST_TIMEOUT_MS,
  });

  if (targets === undefined) return undefined;

  const preferred = selectPreferredImageMimeType(splitLines(targets.toString("utf-8")));
  if (!preferred) return null;

  const data = await deps.run("xclip", ["-selection", "clipboard", "-t", preferred, "-o"]);
  if (data === undefined) return undefined;
  if (data.length === 0) return null;
  return { bytes: data, mimeType: baseMimeType(preferred) };
}

async function readClipboardImageViaNativeClipboard(
  deps: ClipboardDeps,
): Promise<ClipboardImage | null | undefined> {
  const bytes = await deps.nativeClipboard()?.getImage();
  if (bytes === undefined) return undefined;
  if (!bytes?.length) return null;
  return { bytes, mimeType: detectSupportedImageMimeType(bytes) ?? "application/octet-stream" };
}

/**
 * 读一张剪贴板图片：`null` = 没有可用图片（含本仓不收的格式），**不抛**。
 * 值形状与 Pi 上游一致（`{bytes, mimeType}`），字节原样交给上层流式上传。
 */
export async function readClipboardImage(
  options: ClipboardImageOptions = {},
): Promise<ClipboardImage | null> {
  const env = options.env ?? process.env;
  const platform = options.platform ?? process.platform;
  const deps: ClipboardDeps = {
    run: options.run ?? runClipboardCommand,
    nativeClipboard: options.nativeClipboard ?? getNativeClipboard,
    tmpDir: options.tmpDir ?? tmpdir(),
  };

  if (env["TERMUX_VERSION"]) {
    return null;
  }

  let image: ClipboardImage | null | undefined;

  if (platform === "linux") {
    const wsl = isWSL(env);
    if (isWaylandSession(env) || wsl) {
      image = await readClipboardImageViaWlPaste(deps);
    }
    if (image === undefined) image = await readClipboardImageViaXclip(deps);
    // Preserve Linux's empty/unavailable distinction if Windows has no image.
    if (!image && wsl) image = (await readClipboardImageViaPowerShell(deps)) ?? image;
    if (image === undefined) image = await readClipboardImageViaNativeClipboard(deps);
  } else {
    image = await readClipboardImageViaNativeClipboard(deps);
  }

  if (!image) {
    return null;
  }

  // 本仓不接受转码：不支持的格式按"没有可用图片"处理（偏离 1，见文件头）。
  if (!isSupportedImageMimeType(image.mimeType)) {
    return null;
  }

  return image;
}
