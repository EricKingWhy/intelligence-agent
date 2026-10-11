/**
 * 剪贴板取图平台阶梯（#827 MM-06 / AC1；WSL 经 `powershell.exe` 取 Windows 侧剪贴板）。
 *
 * COPY/ADAPT 自 Pi `packages/coding-agent/src/utils/clipboard-image.ts` @ `28dcce2b`
 * （见文件头）。**Linux 沙箱里没有 Windows 剪贴板**，所以 powershell 分支用**注入的假
 * 子进程读取器**驱动：这是本票登记的 Windows 验证缺口，不是假装测过真机。
 */
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  clipboardImageBindings,
  isWaylandSession,
  readClipboardImage,
  type ClipboardImageRun,
} from "../src/lib/clipboard-image.ts";
import { PNG_BYTES as PNG } from "./fixtures.ts";

test("Termux：直接放弃（无系统剪贴板集成）", async () => {
  const calls: string[] = [];
  const run: ClipboardImageRun = async (command) => {
    calls.push(command);
    return undefined;
  };
  const image = await readClipboardImage({
    env: { TERMUX_VERSION: "1" },
    platform: "linux",
    run,
  });
  assert.equal(image, null);
  assert.deepEqual(calls, [], "Termux 分支不应起任何子进程");
});

test("Linux + Wayland：wl-paste 列出类型后按首选类型取字节", async () => {
  const seen: string[][] = [];
  const run: ClipboardImageRun = async (command, args) => {
    seen.push([command, ...args]);
    if (command === "wl-paste" && args[0] === "--list-types") {
      return Buffer.from("text/plain\nimage/jpeg\nimage/png\n");
    }
    if (command === "wl-paste") return Buffer.from(PNG);
    return undefined;
  };
  const image = await readClipboardImage({
    env: { WAYLAND_DISPLAY: "wayland-0" },
    platform: "linux",
    run,
  });
  assert.equal(image?.mimeType, "image/png");
  assert.deepEqual([...image!.bytes], [...PNG]);
  assert.deepEqual(seen[1], ["wl-paste", "--type", "image/png", "--no-newline"]);
});

test("Linux + X11（非 WSL）：走 xclip 的 TARGETS → 数据两跳", async () => {
  const seen: string[][] = [];
  const run: ClipboardImageRun = async (command, args) => {
    seen.push([command, ...args]);
    if (command === "xclip" && args.includes("TARGETS")) return Buffer.from("image/png\n");
    if (command === "xclip") return Buffer.from(PNG);
    return undefined;
  };
  const image = await readClipboardImage({ env: {}, platform: "linux", run });
  assert.equal(image?.mimeType, "image/png");
  assert.ok(!seen.some((c) => c[0] === "wl-paste"), "非 Wayland 不应尝试 wl-paste");
});

test("WSL：Linux 剪贴板无图时回落 powershell.exe 取 Windows 侧剪贴板", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ia-tui-clip-"));
  try {
    const seen: string[] = [];
    const run: ClipboardImageRun = async (command, args) => {
      seen.push(command);
      if (command === "wl-paste" || command === "xclip") return undefined; // 本机剪贴板不可用
      if (command === "wslpath") return Buffer.from(args[1] ?? ""); // 恒等映射，便于本地落盘
      if (command === "powershell.exe") {
        const script = args[args.length - 1] ?? "";
        const match = /\$path = '([^']*)'/.exec(script);
        assert.ok(match, "powershell 脚本必须把目标路径写成字面量");
        writeFileSync(match[1]!, PNG);
        return Buffer.from("ok\n");
      }
      return undefined;
    };
    const image = await readClipboardImage({
      env: { WSL_DISTRO_NAME: "Ubuntu" },
      platform: "linux",
      run,
      tmpDir: dir,
    });
    assert.equal(image?.mimeType, "image/png");
    assert.deepEqual([...image!.bytes], [...PNG]);
    assert.ok(seen.includes("powershell.exe"), "WSL 双路径必须真的走 powershell.exe");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("WSL：Windows 侧也没有图（powershell 回 empty）⇒ null", async () => {
  const dir = mkdtempSync(join(tmpdir(), "ia-tui-clip-"));
  try {
    const run: ClipboardImageRun = async (command, args) => {
      if (command === "wslpath") return Buffer.from(args[1] ?? "");
      if (command === "powershell.exe") return Buffer.from("empty\n");
      return undefined;
    };
    const image = await readClipboardImage({
      env: { WSL_DISTRO_NAME: "Ubuntu" },
      platform: "linux",
      run,
      tmpDir: dir,
    });
    assert.equal(image, null);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("win32 / darwin：走 pi-tui 原生剪贴板（REUSE，不复制 win32 预编译）", async () => {
  const image = await readClipboardImage({
    env: {},
    platform: "win32",
    nativeClipboard: () => ({ getText: async () => null, getImage: async () => PNG }),
  });
  assert.equal(image?.mimeType, "image/png");
});

test("原生剪贴板不可用（undefined）⇒ null（不猜、不抛）", async () => {
  const image = await readClipboardImage({
    env: {},
    platform: "darwin",
    nativeClipboard: () => undefined,
  });
  assert.equal(image, null);
});

test("原生剪贴板返回空 ⇒ null", async () => {
  const image = await readClipboardImage({
    env: {},
    platform: "darwin",
    nativeClipboard: () => ({ getText: async () => null, getImage: async () => new Uint8Array() }),
  });
  assert.equal(image, null);
});

test("本仓不接受的非视觉格式（BMP）⇒ null（AA 偏离：不引 Photon 转码，见文件头）", async () => {
  const bmp = Uint8Array.from([
    0x42, 0x4d, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x36, 0x00, 0x00, 0x00,
    0x28, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x00,
    0x18, 0x00,
  ]);
  const image = await readClipboardImage({
    env: {},
    platform: "win32",
    nativeClipboard: () => ({ getText: async () => null, getImage: async () => bmp }),
  });
  assert.equal(image, null);
});

test("键位：Windows 用 Alt+V（Ctrl+V 常被终端截获）", () => {
  assert.deepEqual(clipboardImageBindings({ platform: "win32", env: {} }), ["alt+v"]);
});

test("键位：全平台只绑 Alt+V —— WSL 不再绑 Ctrl+V（那是终端自己的粘贴文本键，会吞文本 + 刷噪音）", () => {
  assert.deepEqual(
    clipboardImageBindings({ platform: "linux", env: { WSL_DISTRO_NAME: "Ubuntu" } }),
    ["alt+v"],
  );
});

test("键位：原生 Linux 只绑 Alt+V", () => {
  assert.deepEqual(clipboardImageBindings({ platform: "linux", env: {} }), ["alt+v"]);
});

test("Wayland 判定：WAYLAND_DISPLAY 或 XDG_SESSION_TYPE=wayland", () => {
  assert.equal(isWaylandSession({ WAYLAND_DISPLAY: "wayland-0" }), true);
  assert.equal(isWaylandSession({ XDG_SESSION_TYPE: "wayland" }), true);
  assert.equal(isWaylandSession({ XDG_SESSION_TYPE: "x11" }), false);
});
