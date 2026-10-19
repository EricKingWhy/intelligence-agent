// @vitest-environment jsdom
/** #1016 AC3：`App.handleModelChange` 收到默认条目名时 `selectedModel` 归 `null`
 *  （而非固化成那个默认模型名）。
 *
 *  ── 为什么渲染真 App ─────────────────────────────────────────────────────
 *  `handleModelChange` 是 App 内部的 `useCallback`（`App.tsx`）。把这十几行抄进
 *  测试宿主只测副本。故本文件渲染真 App，只 mock 三处外围：
 *    1. `./lib/api::getModels` —— 喂一份含默认条目的目录；
 *    2. `./hooks/useSession` —— 可控 `selectedId` + 记录 `changeModel` 调用；
 *    3. `./components/ModelPicker` —— 捕获 props 并捕获 `onModelChange`
 *       （它**就是** App 的 `handleModelChange`），据此驱动真实现。
 *  观测点 = 每次渲染传给 ModelPicker 的 `selectedModel` prop（= App 状态）。
 *
 *  ── 判别力 ───────────────────────────────────────────────────────────────
 *  默认名 `mimo-v2.6-flash` 在目录里 `default: true`，且 `changeModel` 回执把
 *  `model_id` 也回填成该名字（复刻真机 `POST /model` 的 `effective_model_id`
 *  回执）。修复后 `selectedModel` 必须**保持 null**（既不在入口固化、也不被回执
 *  覆盖）；修复前它会被固化成 `mimo-v2.6-flash`，续聊即 422。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const DEFAULT_NAME = 'mimo-v2.6-flash';

// ── 观测点：ModelPicker 每次拿到的 props ──
const MP = vi.hoisted(() => ({
  props: [] as {
    models: { name: string }[];
    selectedModel: string | null;
    onModelChange: (name: string | null) => void;
  }[],
}));
vi.mock('./components/ModelPicker', () => ({
  ModelPicker: (props: (typeof MP.props)[number]) => {
    MP.props.push(props);
    return null;
  },
}));

// ── 目录数据：默认条目 + 一条真实 catalog 条目 ──
vi.mock('./lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./lib/api')>();
  return {
    ...actual,
    getModels: async () => [
      { name: DEFAULT_NAME, provider: 'mimo', model: DEFAULT_NAME, default: true },
      { name: 'qwen-plus', provider: 'qwen', model: 'qwen-plus', default: false },
    ],
  };
});

const NOOP = () => {};
// `changeModel` 回执回填请求的 target —— 对默认条目这就复刻真机 `POST /model` 的
// `effective_model_id` 回执（= 默认模型名），正是"固化成名字"那条路径的触发源。
const H = vi.hoisted(() => ({
  changeModel: vi.fn(
    async (_sid: string, provider: string, target: string) => ({
      provider,
      model_id: target,
    }),
  ),
  selectedId: 's1' as string | null,
}));

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
  return {
    ...actual,
    useSession: () => ({
      sessions: [],
      selectedId: H.selectedId,
      conversation: null,
      loadingHistory: false,
      streaming: false,
      reconnecting: false,
      error: null,
      sessionsError: null,
      titlesById: {},
      sendFailure: null,
      runFailedBeforePlan: null,
      recoverState: {
        status: 'idle', message: null, conflict: false, repaired: 0,
        terminalRepaired: false, stillUnterminated: false, stillDangling: false,
        pendingDecisions: null,
      },
      selectSession: NOOP,
      submitTask: NOOP,
      sendMessage: NOOP,
      cancelStream: NOOP,
      removeSession: NOOP,
      setArchived: NOOP,
      recover: NOOP,
      submitDecisions: NOOP,
      refreshSessions: NOOP,
      changeModel: H.changeModel,
      fork: NOOP,
      sendSteer: NOOP,
      flushQueue: NOOP,
      cancelItem: NOOP,
    }),
  };
});

vi.mock('./hooks/useProjects', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useProjects')>();
  return {
    ...actual,
    useProjects: () => ({
      projects: [],
      loadError: null,
      refresh: NOOP,
      actions: new Proxy({}, { get: () => NOOP }) as never,
    }),
  };
});

import App from './App';

let container: HTMLDivElement | null = null;
let root: Root | null = null;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  // jsdom 不实现这三个（`theme.ts` 读 matchMedia；Radix/虚拟列表要 ResizeObserver；
  // 贴底滚动要 scrollIntoView）。缺哪个就在挂载时报错，故一次补齐（同 App.test.tsx）。
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: NOOP, removeEventListener: NOOP,
    addListener: NOOP, removeListener: NOOP, dispatchEvent: () => false,
  }));
  vi.stubGlobal('ResizeObserver', class {
    observe = NOOP; unobserve = NOOP; disconnect = NOOP;
  });
  Element.prototype.scrollIntoView = NOOP;
  // App 还会拉 control catalogs / capabilities / sandbox backends（非本用例观测面）——
  // 统一喂空 JSON，避免落到真实网络。
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.includes('/api/capabilities') ? [] : {};
      return new Response(JSON.stringify(body), {
        status: 200, headers: { 'content-type': 'application/json' },
      });
    }),
  );
  MP.props.length = 0;
  H.changeModel.mockClear();
  H.selectedId = 's1';
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  if (container) container.remove();
  root = null;
  container = null;
});

async function mountAndLoadModels(): Promise<void> {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(<App />);
  });
  // 冲刷 fetchModels 的微任务（getModels 是 async）。
  await act(async () => {});
  await act(async () => {});
}

function latestProps() {
  return MP.props[MP.props.length - 1]!;
}

describe('#1016 — 点默认条目后 selectedModel 归 null（不固化成模型名）', () => {
  it('handleModelChange(默认条目名) → selectedModel 保持 null，且回执不覆盖', async () => {
    await mountAndLoadModels();

    // 目录已到达：ModelPicker 至少渲染过一次，且含默认条目。
    expect(MP.props.length).toBeGreaterThan(0);
    expect(latestProps().models.some((m) => m.name === DEFAULT_NAME)).toBe(true);
    expect(latestProps().selectedModel).toBeNull();

    // 驱动真 handleModelChange：把默认条目名当选中值传进去。
    await act(async () => {
      latestProps().onModelChange(DEFAULT_NAME);
    });

    // 本地状态必须归 null——既不在入口固化，也不被 POST /model 回执覆盖。
    expect(latestProps().selectedModel).toBeNull();
    // 切换请求仍然发出（下一轮生效），provider 取自该条目。
    expect(H.changeModel).toHaveBeenCalledWith('s1', 'mimo', DEFAULT_NAME);
  });

  it('点一级「默认链」（null）→ selectedModel 保持 null', async () => {
    await mountAndLoadModels();
    await act(async () => {
      latestProps().onModelChange(null);
    });
    expect(latestProps().selectedModel).toBeNull();
  });

  it('阴性对照：点真实 catalog 条目 → selectedModel 固化成该条目名', async () => {
    await mountAndLoadModels();
    await act(async () => {
      latestProps().onModelChange('qwen-plus');
    });
    // 真实条目照常固化（changeModel 回执 model_id 只在非 null 选择时采用）
    expect(latestProps().selectedModel).toBe('qwen-plus');
  });
});
