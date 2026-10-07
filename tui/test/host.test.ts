/**
 * 本机服务发现（W-21 D5 / #817）：冷启动 / 附着 / 凭据，全部用注入的 IO 面
 * 单测，不需要真服务。线形常量另有一条契约测试直读服务端源码
 * （`src/agent_harness/host_service.py`）——两份镜像不许漂移。
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  ENDPOINT_FILENAME,
  HOST_CREDENTIALS_ENV,
  HOST_PROTOCOL_VERSION,
  TOKEN_USERNAME_PREFIX,
  WORKSPACE_DIR_ENV,
  authorizedFetch,
  describeService,
  hostCredentialsEnvValue,
  parseHostEndpoint,
  resolveCredentialPath,
  resolveDataRoot,
  resolveLocalService,
  resolvePythonPath,
  resolveUserDataDir,
  type HostDeps,
  type HostEndpointInfo,
  type ServeRequest,
} from "../src/host.ts";

const ENDPOINT: HostEndpointInfo = {
  pid: 4242,
  port: 51234,
  protocol_version: HOST_PROTOCOL_VERSION,
  auth_required: true,
};

interface Fake {
  deps: HostDeps;
  /** 子进程请求（空 = 没起过服务）。 */
  spawns: ServeRequest[];
  /** 假时钟走过的毫秒数。 */
  get clock(): number;
  /** 端点文件是否可读（陈旧文件：文件在但服务不健康）。 */
  endpointPresent: boolean;
  /** 服务是否活着（健康探针 + 冷启动后的就绪判定）。 */
  live: boolean;
  /** 下一次 spawn 立刻报这个错（spawn ENOENT 一类）。 */
  spawnError?: Error;
}

/** 注入面：真文件系统与网络都不碰。 */
function fakeDeps(options: { live?: boolean; endpointPresent?: boolean; token?: string | undefined } = {}): Fake {
  const spawns: ServeRequest[] = [];
  let clock = 0;
  const fake: Fake = {
    spawns,
    get clock() {
      return clock;
    },
    endpointPresent: options.endpointPresent ?? options.live ?? false,
    live: options.live ?? false,
    deps: {
      readEndpoint: async () => (fake.endpointPresent || fake.live ? ENDPOINT : undefined),
      healthOk: async () => fake.live,
      spawnServe: (request) => {
        spawns.push(request);
        const error = fake.spawnError;
        if (error === undefined) fake.live = true;
        return {
          onError: (listener) => {
            if (error !== undefined) listener(error);
          },
        };
      },
      readToken: async () => options.token,
      ensureDir: async () => {},
      sleep: async (ms) => {
        clock += ms;
      },
      now: () => clock,
    },
  };
  return fake;
}

const OPTIONS = {
  root: "C:\\Users\\tester\\AppData\\Roaming\\intelligence-agent\\workspace",
  credentialPath: "C:\\Users\\tester\\AppData\\Roaming\\intelligence-agent\\host-credentials.json",
  pythonPath: "C:\\app\\resources\\python\\python.exe",
};

test("附着：已有健康服务时不起子进程，端口/凭据来自服务端", async () => {
  const fake = fakeDeps({ live: true, token: "tok-1" });
  const service = await resolveLocalService(OPTIONS, fake.deps);
  assert.equal(service.source, "attached");
  assert.equal(service.baseUrl, "http://127.0.0.1:51234");
  assert.equal(service.token, "tok-1");
  assert.deepEqual(fake.spawns, []);
});

test("冷启动：无服务时拉起 detached serve，并把同一份数据根/凭据通道交给子进程", async () => {
  const fake = fakeDeps({ token: "tok-2" });
  const service = await resolveLocalService({ ...OPTIONS, env: { PATH: "x" } }, fake.deps);
  assert.equal(service.source, "started");
  assert.equal(fake.spawns.length, 1);
  const spawned = fake.spawns[0];
  assert.equal(spawned?.pythonPath, OPTIONS.pythonPath);
  assert.equal(spawned?.root, OPTIONS.root);
  assert.equal(spawned?.env[WORKSPACE_DIR_ENV], OPTIONS.root);
  assert.equal(spawned?.env[HOST_CREDENTIALS_ENV], `file:${OPTIONS.credentialPath}`);
});

test("端点文件在但服务不健康（陈旧/异构）→ 当作无服务，冷启动", async () => {
  const fake = fakeDeps({ endpointPresent: true, token: "tok-3" });
  const service = await resolveLocalService({ ...OPTIONS, attachBudgetMs: 500 }, fake.deps);
  assert.equal(service.source, "started");
  assert.equal(fake.spawns.length, 1);
});

test("要求凭据但通道里没有 token → 响亮失败，不自欺地发一串 401", async () => {
  const fake = fakeDeps({ live: true, token: undefined });
  await assert.rejects(resolveLocalService(OPTIONS, fake.deps), /没有本数据根的 host token/);
});

test("auth_required=false 的端点（协议上不该出现）不读凭据", async () => {
  const fake = fakeDeps({ live: true, token: undefined });
  const deps: HostDeps = {
    ...fake.deps,
    readEndpoint: async () => ({ ...ENDPOINT, auth_required: false }),
  };
  const service = await resolveLocalService(OPTIONS, deps);
  assert.equal(service.token, undefined);
});

test("spawn 失败（解释器不存在）立即报出原因，不空等整个预算", async () => {
  const fake = fakeDeps({});
  fake.spawnError = new Error("spawn ENOENT");
  await assert.rejects(resolveLocalService(OPTIONS, fake.deps), /spawn ENOENT/);
  assert.ok(fake.clock < 5_000, `不应空等 30s 冷启动预算，实际 ${String(fake.clock)}ms`);
});

test("端点载荷按协议解析；形状不符一律拒绝", () => {
  assert.deepEqual(parseHostEndpoint({ pid: 1, port: 80, protocol_version: 1, auth_required: true }), {
    pid: 1,
    port: 80,
    protocol_version: 1,
    auth_required: true,
  });
  assert.equal(parseHostEndpoint({ pid: 1, port: 80, protocol_version: 1 }), undefined, "缺 auth_required");
  assert.equal(parseHostEndpoint({ pid: 0, port: 80, protocol_version: 1, auth_required: true }), undefined);
  assert.equal(parseHostEndpoint({ pid: 1, port: 70000, protocol_version: 1, auth_required: true }), undefined);
  assert.equal(parseHostEndpoint("nope"), undefined);
});

test("路径约定：数据根与凭据文件是同一层（与桌面壳同源）", () => {
  const userData = resolveUserDataDir("C:\\Users\\tester\\AppData\\Roaming");
  assert.equal(userData, "C:\\Users\\tester\\AppData\\Roaming\\intelligence-agent");
  assert.equal(resolveDataRoot(userData), `${userData}\\workspace`);
  assert.equal(resolveCredentialPath(userData), `${userData}\\host-credentials.json`);
  assert.equal(hostCredentialsEnvValue("C:\\x\\y.json"), "file:C:\\x\\y.json");
  assert.throws(() => resolveUserDataDir(undefined), /APPDATA/);
});

test("python 解析：内置运行时优先，其次仓库 venv，最后交给 PATH", () => {
  const bundled = "C:\\app\\resources\\python\\python.exe";
  const venv = "C:\\app\\resources\\.venv\\Scripts\\python.exe";
  assert.equal(
    resolvePythonPath({ platform: "win32", resourcesDir: "C:\\app\\resources", env: {}, existsSync: () => true }),
    bundled,
  );
  assert.equal(
    resolvePythonPath({
      platform: "win32",
      resourcesDir: "C:\\app\\resources",
      env: {},
      existsSync: (path) => path === venv,
    }),
    venv,
  );
  assert.equal(
    resolvePythonPath({ platform: "win32", resourcesDir: "C:\\app\\resources", env: {}, existsSync: () => false }),
    "python",
  );
  assert.equal(
    resolvePythonPath({
      platform: "win32",
      resourcesDir: "C:\\app\\resources",
      env: { IA_PYTHON: "C:\\py\\python.exe" },
      existsSync: (path) => path === "C:\\py\\python.exe",
    }),
    "C:\\py\\python.exe",
  );
});

test("authorizedFetch：有 token 才加 Bearer，无 token 原样透传（行为不变）", async () => {
  const seen: Array<RequestInit | undefined> = [];
  const inner: typeof fetch = async (_input, init) => {
    seen.push(init);
    return new Response("{}", { status: 200 });
  };
  await authorizedFetch("tok", inner)("http://127.0.0.1:1/api/sessions");
  await authorizedFetch("tok", inner)("http://127.0.0.1:1/api/sessions", { method: "POST" });
  await authorizedFetch(undefined, inner)("http://127.0.0.1:1/api/sessions");
  assert.deepEqual((seen[0]?.headers as Record<string, string>)["Authorization"], "Bearer tok");
  assert.equal(seen[1]?.method, "POST");
  assert.deepEqual((seen[1]?.headers as Record<string, string>)["Authorization"], "Bearer tok");
  assert.deepEqual(seen[2]?.headers, undefined);
});

test("describeService：附着/冷启动与凭据在场与否都摆在明面上（不打印 token 值）", () => {
  const line = describeService({ baseUrl: "http://127.0.0.1:51234", token: "s3cret", endpoint: ENDPOINT, source: "started" });
  assert.equal(line, "started http://127.0.0.1:51234 (pid 4242, protocol 1, credential present)");
  assert.ok(!line.includes("s3cret"));
});

test("契约：镜像的服务端常量与 host_service.py 一致（两份不许漂移）", () => {
  const source = readFileSync(new URL("../../src/agent_harness/host_service.py", import.meta.url), "utf8");
  assert.match(source, new RegExp(`^HOST_PROTOCOL_VERSION = ${String(HOST_PROTOCOL_VERSION)}$`, "m"));
  assert.match(source, new RegExp(`^ENDPOINT_FILENAME = "${ENDPOINT_FILENAME.replace(".", "\\.")}"$`, "m"));
  assert.match(source, new RegExp(`^_TOKEN_USERNAME_PREFIX = "${TOKEN_USERNAME_PREFIX}"$`, "m"));
  assert.match(source, new RegExp(`^HOST_CREDENTIALS_ENV = "${HOST_CREDENTIALS_ENV}"$`, "m"));
  // token username 算法（realpath + normcase 归一 → sha256 前 16 位）
  assert.match(source, /os\.path\.normcase\(os\.path\.realpath\(os\.fspath\(root\)\)\)/);
  assert.match(source, /hashlib\.sha256\(real\.encode\("utf-8"\)\)\.hexdigest\(\)\[:16\]/);
  // 数据根变量来自 Settings.workspace_dir（pydantic-settings 无前缀 → 同名大写）
  const config = readFileSync(new URL("../../src/agent_harness/config.py", import.meta.url), "utf8");
  assert.match(config, /^    workspace_dir: str = /m);
  assert.equal(WORKSPACE_DIR_ENV, "WORKSPACE_DIR");
});

/**
 * B-4 前置规则（#365）：一个数据根一个写者，客户端只做两件事——附着，或拉起
 * 一个 `serve`（它自己抢 `InstanceLock`）。客户端从不杀服务：退出只发
 * `client-exit`（app.ts 的 quit 路径）。这条测试按结构钉住，因为"某个新模块
 * 顺手 kill 一下"是审查最容易漏的形状。
 */
test("单写者规则：只有 host.ts 能拉子进程，且全树没有终止服务的代码", () => {
  const root = fileURLToPath(new URL("../src", import.meta.url));
  const files: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".ts")) files.push(full);
    }
  };
  walk(root);
  assert.ok(files.length > 5, `应扫到 src 下的全部 .ts，实际 ${String(files.length)}`);
  for (const file of files) {
    const text = readFileSync(file, "utf8");
    assert.ok(!/SIGTERM|SIGKILL|taskkill/i.test(text), `${file} 不得向服务发终止信号`);
    assert.ok(!/\.kill\(/.test(text), `${file} 不得杀进程`);
    if (!file.endsWith("host.ts")) {
      assert.ok(!/node:child_process/.test(text), `${file} 只能由 host.ts 拉起本机服务`);
    }
  }
});
