/**
 * 本机服务发现：TUI 冷启动 / 附着（W-21 D5 / #817）。
 *
 * 服务端权威 `src/agent_harness/host_service.py`；本文件与桌面壳的同一层
 * （`desktop/src/host-protocol.ts` + `host-client.ts` + `service-attach.ts`）
 * 镜像**同一组**常量与线形，不另造协议。
 *
 * 单写者语义（#365 B-4 前置）：一个数据根只有一个服务持 `InstanceLock` 与
 * 会话写入权；桌面与 TUI 都只是客户端：有服务就附着，没有就拉起一个
 * **detached** 的 `agent-harness serve`，而 `serve` 自己再抢锁、抢不到就附着
 * 退出。客户端从不杀服务：退出只发 `client-exit`（ADR-0046），服务继续跑。
 */

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync as defaultExistsSync, realpathSync } from "node:fs";
import { mkdir, readFile } from "node:fs/promises";
import { join } from "node:path";

/** 必须等于 `host_service.HOST_PROTOCOL_VERSION`。 */
export const HOST_PROTOCOL_VERSION = 1;

/** 端点状态文件名（`host_service.ENDPOINT_FILENAME`）。 */
export const ENDPOINT_FILENAME = ".host-service.json";

/** host token 的凭据通道用户名前缀（`host_service._TOKEN_USERNAME_PREFIX`）。 */
export const TOKEN_USERNAME_PREFIX = "host-service/";

/** 凭据后端选择变量（`host_service.HOST_CREDENTIALS_ENV`）。 */
export const HOST_CREDENTIALS_ENV = "AGENT_HARNESS_HOST_CREDENTIALS";

/** 数据根变量：`Settings.workspace_dir`（pydantic-settings，无前缀）。 */
export const WORKSPACE_DIR_ENV = "WORKSPACE_DIR";

/** 端点文件载荷；token 从不落这里（走凭据通道）。 */
export interface HostEndpointInfo {
  readonly pid: number;
  readonly port: number;
  readonly protocol_version: number;
  readonly auth_required: boolean;
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

/** 按协议形状解析端点载荷；形状不符一律 undefined（按无服务处理）。 */
export function parseHostEndpoint(payload: unknown): HostEndpointInfo | undefined {
  if (typeof payload !== "object" || payload === null) return undefined;
  const record = payload as Record<string, unknown>;
  const { pid, port, protocol_version: protocolVersion, auth_required: authRequired } = record;
  if (!isInteger(pid) || pid <= 0) return undefined;
  if (!isInteger(port) || port <= 0 || port > 65535) return undefined;
  if (!isInteger(protocolVersion)) return undefined;
  if (typeof authRequired !== "boolean") return undefined;
  return { pid, port, protocol_version: protocolVersion, auth_required: authRequired };
}

/**
 * 用户数据目录：`%APPDATA%\intelligence-agent`。
 *
 * 与桌面壳同一约定（`desktop/src/installer/paths.ts:26`）；两个客户端必须落到
 * 同一个数据根，否则永远互相看不见（附着失败 = 两个写者）。
 */
export function resolveUserDataDir(appData: string | undefined): string {
  if (appData === undefined || appData.trim() === "") {
    throw new Error("无法确定用户数据目录：APPDATA 未设置（用 --data-root 指定数据根）");
  }
  return join(appData, "intelligence-agent");
}

/** 数据根（`resolveDesktopDataRoot` 同值）：端点文件、实例锁、harness.db 都在其下。 */
export function resolveDataRoot(userDataDir: string): string {
  return join(userDataDir, "workspace");
}

/** 凭据通道文件（`resolveHostCredentialPath` 同值）。 */
export function resolveCredentialPath(userDataDir: string): string {
  return join(userDataDir, "host-credentials.json");
}

/** `AGENT_HARNESS_HOST_CREDENTIALS` 取值（`host_service.default_credentials` 认 `file:` 前缀）。 */
export function hostCredentialsEnvValue(credentialPath: string): string {
  return `file:${credentialPath}`;
}

/**
 * 数据根在凭据通道里的 username（`host_service.host_token_username`）。
 *
 * realpath + 小写归一（Windows `os.path.normcase` 语义），sha256 取前 16 个
 * 十六进制字符；同一数据根的不同路径拼写必须落到同一条凭据上。
 */
export function hostTokenUsername(root: string): string {
  const real = realpathSync(root);
  const normalized = process.platform === "win32" ? real.toLowerCase() : real;
  const digest = createHash("sha256").update(normalized, "utf8").digest("hex").slice(0, 16);
  return `${TOKEN_USERNAME_PREFIX}${digest}`;
}

/** 读取凭据通道里的 host token（文件后端 JSON：{username: token}）。 */
export async function readHostToken(root: string, credentialPath: string): Promise<string | undefined> {
  try {
    const payload: unknown = JSON.parse(await readFile(credentialPath, "utf8"));
    if (typeof payload !== "object" || payload === null) return undefined;
    const token = (payload as Record<string, unknown>)[hostTokenUsername(root)];
    return typeof token === "string" && token.length > 0 ? token : undefined;
  } catch {
    return undefined;
  }
}

/** python 解析依赖（可注入，便于单测）。 */
export interface PythonPathDeps {
  readonly platform: string;
  /** 产物/仓库根：由本模块自身位置推出（打包 `<resources>`，开发 `<repo>`）。 */
  readonly resourcesDir: string;
  readonly env: NodeJS.ProcessEnv;
  readonly existsSync: (path: string) => boolean;
}

/** 本模块所在目录（`<...>/tui/dist/src`）的上层根目录。 */
export function resolveResourcesDir(moduleDir: string): string {
  return join(moduleDir, "..", "..", "..");
}

/**
 * python 解释器：显式覆盖 > 安装包内置运行时 > 仓库 venv > PATH。
 *
 * 打包形态与桌面壳同源（`<resources>/python/python.exe`，installer extraResources）；
 * 开发形态用仓库 `.venv`。找不到就交给 PATH 的 `python`，让失败发生在明处
 * （报错带 spawn 的 ENOENT）。
 */
export function resolvePythonPath(deps: PythonPathDeps): string {
  const explicit = deps.env["IA_PYTHON"];
  if (explicit !== undefined && explicit.trim() !== "" && deps.existsSync(explicit)) return explicit;
  const candidates =
    deps.platform === "win32"
      ? [join(deps.resourcesDir, "python", "python.exe")]
      : [join(deps.resourcesDir, "python", "bin", "python3")];
  candidates.push(join(deps.resourcesDir, ".venv", "Scripts", "python.exe"));
  for (const candidate of candidates) {
    if (deps.existsSync(candidate)) return candidate;
  }
  return "python";
}

/** 解析结果：客户端要打到哪、带什么凭据、服务是怎么来的（附着还是冷启动）。 */
export interface LocalService {
  readonly baseUrl: string;
  readonly token?: string | undefined;
  readonly endpoint: HostEndpointInfo;
  readonly source: "attached" | "started";
}

/** 冷启动时交给子进程的一次请求。 */
export interface ServeRequest {
  readonly pythonPath: string;
  readonly root: string;
  readonly env: NodeJS.ProcessEnv;
}

/** 子进程句柄（只为拿到 spawn 失败 / 提前退出的原因，客户端不管理它的生命周期）。 */
export interface SpawnedServe {
  onError(listener: (error: Error) => void): void;
  /**
   * 子进程退出（#848 R4）。已死的子进程不可能再发布端点：不接这个信号，TUI 会把
   * 「起来了但立刻死了」说成「可能仍在启动」，空等满整个预算。桌面壳对**同一个**
   * 子进程早就这么做（`abortReason`，W-21 D11 / #837），两个客户端不许对同一件事
   * 有两种说法。
   *
   * 附着路径不受影响：子进程抢锁失败、附着到已有服务后正常退出时，端点文件已经在
   * 了，`waitForService` 先读到它并返回，走不到这个中止分支。
   */
  onExit(listener: (code: number | null) => void): void;
}

/** 注入的本机 IO 面（单测不需要真服务）。 */
export interface HostDeps {
  readEndpoint(root: string): Promise<HostEndpointInfo | undefined>;
  /** 一级探针：`GET /api/health` 200 + `status: ok` + 协议版本一致。 */
  healthOk(port: number): Promise<boolean>;
  spawnServe(request: ServeRequest): SpawnedServe;
  readToken(root: string, credentialPath: string): Promise<string | undefined>;
  ensureDir(path: string): Promise<void>;
  sleep(ms: number): Promise<void>;
  now(): number;
}

/** 解析选项。 */
export interface ResolveOptions {
  readonly root: string;
  readonly credentialPath: string;
  readonly pythonPath: string;
  readonly env?: NodeJS.ProcessEnv;
  readonly attachBudgetMs?: number;
  readonly startBudgetMs?: number;
}

const ATTACH_BUDGET_MS = 2_000;
/**
 * 冷启动预算（#846）。装完安装件后的**首次**启动实测 35.7 s（`--check`）/ 36-42 s
 * （真实附着），热态 6.6-6.9 s；桌面壳在同一台机器上还记过更差的一次：装完安装件
 * 后的**第一次**运行 "not within 60 s"（`desktop/src/service-host.ts`）。两条读数
 * 不冲突，是同一现象的两端：30 s 窗口会把「只是慢」判成失败，而失败后那个服务
 * 仍会自己就绪、变成没有客户端的孤儿。
 *
 * 取 90 s 与桌面壳对**同一个子进程**的 `DEFAULT_START_BUDGET_MS` 对齐
 * （W-21 D11 / #837）：两个客户端不许对同一件事各有一套预算。**文案里不复述任何
 * 秒数**（#848 R5）：写死的读数一旦与另一侧的记录对不上，用户读到的就是错的。
 * 客户端**不**在超时后杀掉这个子进程（它可能已被别的客户端附着，见
 * test/host.test.ts 的单写者结构守卫）。
 */
const START_BUDGET_MS = 90_000;
const POLL_INTERVAL_MS = 250;
const HEALTH_PROBE_TIMEOUT_MS = 1_500;
const TOKEN_READ_ATTEMPTS = 20;
const TOKEN_READ_INTERVAL_MS = 100;

/**
 * 等一个可附着的服务：端点文件 + 健康探针都成立才算附着。
 *
 * 只有一级健康探针（桌面壳的 TCP 级在这里是多余的）：端口是服务自己拿的
 * `port=0` 随机端口，从端点文件读出来，"不是我们的服务" 与 "没人应答" 走
 * 同一条失败路径，而失败总是安全的（`serve` 自己抢锁，抢不到会附着退出，
 * 绝不出现第二个写者）。
 */
async function waitForService(
  root: string,
  deps: HostDeps,
  budgetMs: number,
  abortReason?: () => Error | undefined,
): Promise<HostEndpointInfo | undefined> {
  const deadline = deps.now() + budgetMs;
  for (;;) {
    const endpoint = await deps.readEndpoint(root);
    if (endpoint !== undefined && (await deps.healthOk(endpoint.port))) return endpoint;
    const abort = abortReason?.();
    if (abort !== undefined) throw abort;
    if (deps.now() >= deadline) return undefined;
    await deps.sleep(POLL_INTERVAL_MS);
  }
}

/** 读 token；服务在发布端点文件之前就写好 token，短暂重试只为对齐文件系统。 */
async function readTokenWithRetry(
  root: string,
  credentialPath: string,
  deps: HostDeps,
): Promise<string | undefined> {
  for (let attempt = 0; attempt < TOKEN_READ_ATTEMPTS; attempt += 1) {
    const token = await deps.readToken(root, credentialPath);
    if (token !== undefined) return token;
    await deps.sleep(TOKEN_READ_INTERVAL_MS);
  }
  return undefined;
}

/**
 * 拿到一个可用服务：有就附着，没有就 detached 拉起 `agent-harness serve` 再等。
 *
 * 凭据（AC#3）：服务永远 `auth_required=true`，token 写在双方约定的凭据通道里
 * （`AGENT_HARNESS_HOST_CREDENTIALS=file:<path>`，两个客户端都给子进程同一个值）。
 * 取不到 token 就**响亮失败**：绝不发一串注定 401 的请求掩盖原因。
 */
export async function resolveLocalService(
  options: ResolveOptions,
  deps: HostDeps,
): Promise<LocalService> {
  const env = options.env ?? process.env;
  const attachBudgetMs = options.attachBudgetMs ?? ATTACH_BUDGET_MS;
  let source: LocalService["source"] = "attached";
  let endpoint = await waitForService(options.root, deps, attachBudgetMs);
  if (endpoint === undefined) {
    source = "started";
    // 子进程 cwd = 数据根：spawn 前必须存在（不然 ENOENT，服务来不及建）。
    await deps.ensureDir(options.root);
    let spawnError: Error | undefined;
    let exitCode: number | null | undefined;
    const child = deps.spawnServe({
      pythonPath: options.pythonPath,
      root: options.root,
      env: {
        ...env,
        [WORKSPACE_DIR_ENV]: options.root,
        [HOST_CREDENTIALS_ENV]: hostCredentialsEnvValue(options.credentialPath),
      },
    });
    child.onError((error) => {
      spawnError ??= error;
    });
    child.onExit((code) => {
      exitCode ??= code;
    });
    endpoint = await waitForService(
      options.root,
      deps,
      options.startBudgetMs ?? START_BUDGET_MS,
      () =>
        spawnError ??
        (exitCode === undefined
          ? undefined
          : new Error(
              `本机服务进程在就绪前退出（exit code ${String(exitCode)}）：子进程已死，` +
                "再等也不会发布端点",
            )),
    );
    if (endpoint === undefined) {
      throw new Error(
        `本机服务 ${String((options.startBudgetMs ?? START_BUDGET_MS) / 1000)}s 内未就绪（数据根 ${options.root}）：` +
          "若安装刚完成，首次冷启动可能明显慢于热态，该服务可能仍在启动，稍后重试即可附着它；" +
          `否则检查 ${options.pythonPath} 能否运行 \`-m agent_harness.cli serve\``,
      );
    }
  }
  let token: string | undefined;
  if (endpoint.auth_required) {
    token = await readTokenWithRetry(options.root, options.credentialPath, deps);
    if (token === undefined) {
      throw new Error(
        `运行中的服务要求凭据，但 ${options.credentialPath} 里没有本数据根的 host token：` +
          "该服务是用别的凭据通道（如系统凭据管理器）启动的；" +
          "请用桌面或本 TUI 启动服务，让两端共用同一个通道",
      );
    }
  }
  return {
    baseUrl: `http://127.0.0.1:${String(endpoint.port)}`,
    token,
    endpoint,
    source,
  };
}

/** 默认 IO 面：真文件系统 + 全局 fetch + detached spawn。 */
export function defaultHostDeps(): HostDeps {
  return {
    async readEndpoint(root: string): Promise<HostEndpointInfo | undefined> {
      try {
        const text = await readFile(join(root, ENDPOINT_FILENAME), "utf8");
        return parseHostEndpoint(JSON.parse(text));
      } catch {
        return undefined;
      }
    },
    async healthOk(port: number): Promise<boolean> {
      try {
        const response = await fetch(`http://127.0.0.1:${String(port)}/api/health`, {
          signal: AbortSignal.timeout(HEALTH_PROBE_TIMEOUT_MS),
        });
        if (response.status !== 200) return false;
        const body: unknown = await response.json();
        if (typeof body !== "object" || body === null) return false;
        const record = body as Record<string, unknown>;
        return record.status === "ok" && record.protocol_version === HOST_PROTOCOL_VERSION;
      } catch {
        return false;
      }
    },
    spawnServe(request: ServeRequest): SpawnedServe {
      // detached + stdio ignore：客户端退出后服务继续跑（一个数据根一个写者，
      // 谁先起来谁持有；后到的附着）。就绪靠端点文件 + 健康探针，不读 stdout。
      const child = spawn(request.pythonPath, ["-m", "agent_harness.cli", "serve", "--host", "127.0.0.1"], {
        cwd: request.root,
        env: request.env,
        detached: true,
        windowsHide: true,
        stdio: "ignore",
      });
      child.unref();
      return {
        onError: (listener) => {
          child.once("error", listener);
        },
        onExit: (listener) => {
          child.once("exit", listener);
        },
      };
    },
    readToken: readHostToken,
    ensureDir: async (path: string) => {
      await mkdir(path, { recursive: true });
    },
    // 故意不 unref（与桌面壳的 defaultSleep 不同）：TUI 的事件循环只靠这个
    // 定时器撑着，"等就绪" 期间没有别的句柄，unref 会让进程静默退出（exit 0）。
    sleep: (ms: number) =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, ms);
      }),
    now: () => Date.now(),
  };
}

/**
 * 带上 Bearer 的 fetch（无 token 时原样返回，行为与改造前完全一致）。
 *
 * 服务端是非 fail-open 的（`serve` 每次都签发限权 token），所以 TUI 的每个
 * API 调用（REST、SSE、client-exit）都走这里。
 */
export function authorizedFetch(
  token: string | undefined,
  fetchImpl: typeof fetch = fetch,
): typeof fetch {
  if (token === undefined) return fetchImpl;
  return (input, init) =>
    fetchImpl(input, {
      ...init,
      headers: { ...((init?.headers ?? {}) as Record<string, string>), Authorization: `Bearer ${token}` },
    });
}

/** 供 --check 打印的一行事实（不打印 token 本身）。 */
export function describeService(service: LocalService): string {
  const endpoint = service.endpoint;
  const label = service.source === "attached" ? "attached" : "started";
  return `${label} ${service.baseUrl} (pid ${String(endpoint.pid)}, protocol ${String(endpoint.protocol_version)}, credential ${service.token === undefined ? "none" : "present"})`;
}
