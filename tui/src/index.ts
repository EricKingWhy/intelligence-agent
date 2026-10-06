/**
 * ia-tui 入口：短任务一条命令进入。
 *   ia-tui --server http://127.0.0.1:8000 --session <id>
 * 非 TTY 环境直接报错退出（TUI 需要 PTY；批准问答的非 TTY 默认拒绝语义
 * 由 approvalDecision 的纯函数覆盖，见 views/approval.ts）。
 */
import { TuiApp } from "./app.ts";

interface Args {
  baseUrl: string;
  sessionId: string | null;
}

function parseArgs(argv: string[]): Args {
  let baseUrl = process.env["IA_SERVER"] ?? "http://127.0.0.1:8000";
  let sessionId: string | null = null;
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--server" && argv[i + 1] !== undefined) {
      baseUrl = argv[i + 1] as string;
      i += 1;
    } else if (arg === "--session" && argv[i + 1] !== undefined) {
      sessionId = argv[i + 1] as string;
      i += 1;
    }
  }
  return { baseUrl, sessionId };
}

async function main(): Promise<void> {
  const { baseUrl, sessionId } = parseArgs(process.argv.slice(2));
  if (!sessionId) {
    process.stderr.write("usage: ia-tui --session <id> [--server <url>]\n");
    process.exit(2);
  }
  if (!process.stdin.isTTY) {
    process.stderr.write("ia-tui 需要 TTY 运行（管道/重定向环境不支持交互界面）\n");
    process.exit(2);
  }
  const app = new TuiApp({ baseUrl, sessionId });
  await app.start();
}

main().catch((error: unknown) => {
  process.stderr.write(`ia-tui failed: ${String(error)}\n`);
  process.exit(1);
});
