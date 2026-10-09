/**
 * 子进程读剪贴板（零依赖）。
 *
 * 来源：Pi `packages/coding-agent/src/utils/clipboard-command.ts:1-44` @ commit
 * `28dcce2ba45ce4a9efeb0f5b686f0be830fd89b9`（MIT，许可全文见
 * `tui/THIRD_PARTY_NOTICES.md`）。行为 **ADAPT**（对外语义逐条保留，实现换成本仓
 * 的规矩，理由见下）：
 *
 * 1. **超时收口交给 `execFile({timeout})`** 而不是手写 `spawn` + `setTimeout` +
 *    显式杀子进程。本仓有一条结构守卫（`tui/test/host.test.ts` 的"单写者规则"，
 *    #365）禁止 `src/` 里出现任何终止信号的形状：客户端的子进程只能靠 Node 自己的
 *    超时机制收口，绝不手搓 kill（"某个新模块顺手 kill 一下"正是那条守卫要挡的形状）。
 *    对外行为不变：超时/超长输出/非零退出/命令不存在 => `undefined`。
 * 2. **砍掉上游的 `input` 选项**：剪贴板阶梯里没有任何一跳需要往子进程写 stdin
 *    （`wl-paste` / `xclip` / `powershell.exe` 都只读剪贴板），本仓不留未用的参数。
 *
 * 语义要点（审查时不要"简化"掉的那两条）：
 * - `undefined` = **命令失败**；空 Buffer = **成功但无数据** -- 剪贴板读取必须能区分
 *   "读不到"与"没有图"（AC1 的 `empty` 与失败走不同分支）；
 * - `maxBuffer` 必须显式给足上限：一张 4K 截图的 PNG 就有几 MB，Node 缺省 1MB 会
 *   把正常图片判成"读不出来"。
 */
import { execFile } from "node:child_process";

export interface ClipboardCommandOptions {
  timeoutMs?: number;
  maxBufferBytes?: number;
}

/** 剪贴板工具的缺省墙钟上限：X/Wayland 连接挂住是常态，不能等它自己超时。 */
const DEFAULT_TIMEOUT_MS = 3000;

/** 缺省输出上限（对上：单张图片的服务端上限量级；对下：远超 Node 的 1MB 缺省）。 */
const DEFAULT_MAX_BUFFER_BYTES = 50 * 1024 * 1024;

/** `undefined` = 命令失败；空 Buffer = 成功但无输出。 */
export function runClipboardCommand(
  command: string,
  args: readonly string[],
  options?: ClipboardCommandOptions,
): Promise<Buffer | undefined> {
  const { promise, resolve } = Promise.withResolvers<Buffer | undefined>();
  execFile(
    command,
    [...args],
    {
      timeout: options?.timeoutMs ?? DEFAULT_TIMEOUT_MS,
      maxBuffer: options?.maxBufferBytes ?? DEFAULT_MAX_BUFFER_BYTES,
      windowsHide: true,
    },
    (error, stdout) => {
      if (error !== null) {
        resolve(undefined);
        return;
      }
      resolve(Buffer.isBuffer(stdout) ? stdout : Buffer.from(stdout));
    },
  );
  return promise;
}
