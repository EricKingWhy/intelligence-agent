/**
 * W-21 #847: exercise the actual TUI --check path through the shipped Windows launcher.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, linkSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";

const LAUNCHER = fileURLToPath(new URL("../../desktop/installer/ia-tui.cmd", import.meta.url));
const TUI_ENTRY = fileURLToPath(new URL("../src/index.ts", import.meta.url));

test("ia-tui.cmd --check forwards real TUI success and unavailable-service exit codes", {
  skip: process.platform === "win32" ? false : "cmd.exe is available only on Windows",
}, () => {
  const root = mkdtempSync(join(tmpdir(), "ia-tui-check-"));
  const launcherPath = join(root, "ia-tui.cmd");
  const nodePath = join(root, "resources", "node", "node.exe");
  const entryPath = join(root, "resources", "tui", "dist", "src", "index.js");

  try {
    mkdirSync(dirname(nodePath), { recursive: true });
    mkdirSync(dirname(entryPath), { recursive: true });
    try {
      linkSync(process.execPath, nodePath);
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== "EXDEV" && code !== "EPERM") throw error;
      copyFileSync(process.execPath, nodePath);
    }
    writeFileSync(launcherPath, readFileSync(LAUNCHER));
    writeFileSync(
      entryPath,
      [
        "globalThis.fetch = async (input) => {",
        "  const url = new URL(String(input));",
        "  if (url.pathname === '/api/health') return new Response(JSON.stringify({ status: 'ok', protocol_version: 1 }));",
        "  if (url.pathname === '/api/sessions') return new Response('[]', { status: Number(process.env.TUI_TEST_SESSIONS_STATUS ?? 200) });",
        "  throw new Error('unexpected URL: ' + url.href);",
        "};",
        "void import(" + JSON.stringify(pathToFileURL(TUI_ENTRY).href) + ");",
        "",
      ].join("\n"),
    );

    const appData = join(root, "app-data");
    const dataRoot = join(appData, "intelligence-agent", "workspace");
    mkdirSync(dataRoot, { recursive: true });
    writeFileSync(
      join(dataRoot, ".host-service.json"),
      JSON.stringify({ pid: process.pid, port: 43210, protocol_version: 1, auth_required: false }),
    );

    const runCheck = (overrides: NodeJS.ProcessEnv) =>
      spawnSync(process.env.ComSpec ?? "cmd.exe", ["/d", "/c", "call ia-tui.cmd --check"], {
        cwd: root,
        encoding: "utf8",
        env: {
          ...process.env,
          ...overrides,
          ERRORLEVEL: "77",
          NODE_OPTIONS: "--experimental-transform-types",
        },
        timeout: 30_000,
        windowsHide: true,
      });

    const healthy = runCheck({ APPDATA: appData });
    assert.equal(healthy.error, undefined, healthy.error?.message ?? "");
    assert.equal(healthy.status, 0, healthy.stderr ?? "");
    assert.match(healthy.stdout, /\/api\/sessions .*HTTP 200/);

    const unavailable = runCheck({
      APPDATA: join(root, "unavailable-app-data"),
      IA_PYTHON: process.execPath,
    });
    assert.equal(unavailable.error, undefined, unavailable.error?.message ?? "");
    assert.equal(unavailable.status, 1, unavailable.stderr ?? "");
    assert.match(unavailable.stderr, /本机服务进程在就绪前退出|内未就绪/);
  } finally {
    rmSync(root, { force: true, recursive: true });
  }
});
