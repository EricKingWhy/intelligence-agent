/**
 * ia-tui 入口。
 *   ia-tui --session <id>                 # 本机服务：有就附着，没有就冷启动
 *   ia-tui --session new                  # 先建一个会话再进界面（冷启动从零开始）
 *   ia-tui --session <id> --server <url>  # 显式指向一个服务（开发用，跳过发现）
 *   ia-tui --check                        # 只解析/冷启动服务并打印事实，不进界面
 * 非 TTY 环境直接报错退出（TUI 需要 PTY；批准问答的非 TTY 默认拒绝语义
 * 由 approvalDecision 的纯函数覆盖，见 views/approval.ts）。
 */
import { ApiClient } from "./api.ts";
import { TuiApp } from "./app.ts";
import {
  authorizedFetch,
  defaultHostDeps,
  describeService,
  resolveCredentialPath,
  resolveDataRoot,
  resolveLocalService,
  resolvePythonPath,
  resolveResourcesDir,
  resolveUserDataDir,
  type LocalService,
} from "./host.ts";
import { existsSync } from "node:fs";
import { dirname } from "node:path";

interface Args {
  /** 显式服务地址（`--server` 或 `IA_SERVER`）；null = 走本机发现。 */
  baseUrl: string | null;
  sessionId: string | null;
  dataRoot: string | null;
  check: boolean;
}

const USAGE = [
  "usage: ia-tui --session <id|new> [--data-root <dir>] [--server <url>]",
  "       ia-tui --check [--data-root <dir>]",
  "  --session <id|new>  会话 id；`new` = 先建一个会话再进界面",
  "  --data-root <dir>   数据根（默认 %APPDATA%\\intelligence-agent\\workspace，与桌面同源）",
  "  --server <url>      显式指向服务（开发用；跳过发现，不带凭据）",
  "  --check             解析/冷启动本机服务并打印事实（凭据是否可用），不进入界面",
].join("\n");

function parseArgs(argv: string[]): Args {
  const args: Args = {
    baseUrl: process.env["IA_SERVER"] ?? null,
    sessionId: null,
    dataRoot: null,
    check: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const value = argv[i + 1];
    if (arg === "--check") {
      args.check = true;
    } else if (arg === "--server" && value !== undefined) {
      args.baseUrl = value;
      i += 1;
    } else if (arg === "--session" && value !== undefined) {
      args.sessionId = value;
      i += 1;
    } else if (arg === "--data-root" && value !== undefined) {
      args.dataRoot = value;
      i += 1;
    } else {
      throw new Error(`unknown argument: ${String(arg)}\n${USAGE}`);
    }
  }
  return args;
}

/** 数据根：显式 `--data-root`，否则 `%APPDATA%\intelligence-agent\workspace`（与桌面同源）。 */
function resolveRoot(args: Args): string {
  if (args.dataRoot !== null) return args.dataRoot;
  return resolveDataRoot(resolveUserDataDir(process.env["APPDATA"]));
}

/**
 * 本机服务（W-21 D5 / #817）：附着已有服务，没有就冷启动一个并等它就绪。
 * 凭据通道文件与数据根同层（`%APPDATA%\intelligence-agent\`），两个客户端同值。
 */
async function resolveLocal(args: Args): Promise<{ service: LocalService; root: string; credentialPath: string }> {
  const root = resolveRoot(args);
  const credentialPath = resolveCredentialPath(dirname(root));
  const service = await resolveLocalService(
    {
      root,
      credentialPath,
      pythonPath: resolvePythonPath({
        platform: process.platform,
        resourcesDir: resolveResourcesDir(import.meta.dirname),
        env: process.env,
        existsSync,
      }),
    },
    defaultHostDeps(),
  );
  return { service, root, credentialPath };
}

/** 写一行并等它真的写出（`process.exit` 会截断未 flush 的管道写，证据行不能丢）。 */
function writeAndFlush(stream: NodeJS.WriteStream, text: string): Promise<void> {
  return new Promise((resolve) => {
    stream.write(text, () => {
      resolve();
    });
  });
}

/** `--check`：把解析结果与凭据可用性摆到明面上（AC#2/#3 的实机证据，不打印 token）。 */
async function runCheck(args: Args): Promise<number> {
  const { service, root, credentialPath } = await resolveLocal(args);
  const lines = [
    `service: ${describeService(service)}`,
    `data root: ${root}`,
    `credential file: ${credentialPath}`,
  ];
  const sessionsPath = "/api/sessions";
  const withCredential = await fetch(`${service.baseUrl}${sessionsPath}`, {
    headers: service.token === undefined ? {} : { Authorization: `Bearer ${service.token}` },
  });
  lines.push(`api: GET ${sessionsPath} 带凭据 -> HTTP ${String(withCredential.status)}`);
  if (service.token !== undefined) {
    const withoutCredential = await fetch(`${service.baseUrl}${sessionsPath}`);
    lines.push(`api: GET ${sessionsPath} 不带凭据 -> HTTP ${String(withoutCredential.status)}`);
  }
  await writeAndFlush(process.stdout, `${lines.join("\n")}\n`);
  if (!withCredential.ok) {
    await writeAndFlush(
      process.stderr,
      `ia-tui --check 失败：带凭据的请求未通过（HTTP ${String(withCredential.status)}）。` +
        "服务端是非 fail-open 的，请确认服务是由桌面/本 TUI 用同一个凭据通道启动的。\n",
    );
    return 1;
  }
  return 0;
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  if (args.check) {
    process.exit(await runCheck(args));
  }
  let baseUrl: string;
  let token: string | undefined;
  if (args.baseUrl !== null) {
    baseUrl = args.baseUrl;
  } else {
    const local = await resolveLocal(args);
    baseUrl = local.service.baseUrl;
    token = local.service.token;
  }
  if (!args.sessionId) {
    process.stderr.write(`${USAGE}\n`);
    process.exit(2);
  }
  if (!process.stdin.isTTY) {
    process.stderr.write("ia-tui 需要 TTY 运行（管道/重定向环境不支持交互界面）\n");
    process.exit(2);
  }
  let sessionId = args.sessionId;
  if (sessionId === "new") {
    const created = await new ApiClient(baseUrl, authorizedFetch(token)).createSession();
    const createdId = created.session_id;
    if (typeof createdId !== "string" || createdId === "") {
      throw new Error("服务端未返回 session_id");
    }
    sessionId = createdId;
  }
  const app = new TuiApp({ baseUrl, token, sessionId });
  await app.start();
}

main().catch((error: unknown) => {
  process.stderr.write(`ia-tui failed: ${String(error)}\n`);
  process.exit(1);
});
