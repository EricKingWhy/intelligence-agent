// @vitest-environment jsdom
/** #1195 — Composer 内联 `/` 命令菜单（集成层，jsdom 行为测试）。
 *
 *  判据逐条对票面 §四：
 *   1. 行首/空格后 `/` → 菜单开（role=listbox），正上方内联（非 Dialog）；
 *   2. 打字 fuzzy 过滤、空态「无匹配命令」；
 *   3. ↑↓ 导航、Enter 执行、Esc 关闭；菜单开时 ↑↓ 不移光标（preventDefault）；
 *   4. 唯一命中 Enter 直接执行；
 *   5. 带参 `/goal <文本>` 提交拦截：preventDefault、不 onSubmit、调 API；
 *   6. 全角 ／ 不触发；无会话（sessionId=null）菜单不触发；
 *   7. API 失败（409/422）错误可见（reportError 面），不静默吞。
 *
 *  mock 面：`lib/api`（compactSession/getTaskState 传输）+ `lib/auth`（getToken）。
 *  slashCommands 的 sessionFetch 走全局 fetch ⇒ 同样 mock（Workflow-mode / task
 *  definition 两个端点没有 client，fetch 是真实出口）。
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    compactSession: vi.fn(),
    getTaskState: vi.fn(),
    getSlashRegistry: vi.fn(),
  };
});
vi.mock('../lib/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/auth')>();
  return { ...actual, getToken: vi.fn(() => null) };
});

import {
  compactSession,
  getSlashRegistry,
  getTaskState,
  type SessionContextCompacted,
  type SlashRegistryCommand,
  type TaskState,
} from '../lib/api';
import { Composer } from './Composer';

type SubmitArgs = [task: string, rememberAsProceduralRule: boolean, attachmentIds?: string[], scope?: 'session' | 'project' | 'global'];

let container: HTMLDivElement;
let root: Root;
let onSubmit: Mock<(...args: SubmitArgs) => void>;
let onSteer: Mock<(...args: SubmitArgs) => void>;
let fetchMock: Mock;

/** #1200：后端 registry 的固定投影（5 builtin + 3 skill，含插件命名空间）。 */
const REGISTRY: SlashRegistryCommand[] = [
  { name: 'goal', kind: 'builtin', description: '设置或查看长期任务目标' },
  { name: 'plan', kind: 'builtin', description: '进入或退出计划模式' },
  { name: 'compact', kind: 'builtin', description: '压缩以上对话内容' },
  { name: 'permission', kind: 'builtin', description: '切换权限预设（沙箱模式与审批策略）' },
  { name: 'model', kind: 'builtin', description: '选择本会话使用的模型' },
  { name: 'deep-research', kind: 'skill', description: '深度调研', argumentHint: '主题…' },
  { name: 'api-design', kind: 'skill', description: 'REST API 设计审查' },
  { name: 'code-review', kind: 'skill', description: '双轴代码审查', namespace: 'acme' },
];

const taskState = (text: string | null): TaskState => ({
  defined: text !== null,
  task_text: text,
  read_write_intent: null,
  cwd: null,
  authorization: null,
  criteria: [],
  verification: {},
  acceptance: null,
  version: 1,
  open_run_ids: [],
  product_state: '',
});

const textarea = (): HTMLTextAreaElement => container.querySelector('#composer-input')!;
const menu = (): HTMLElement | null => container.querySelector('[data-testid=slash-menu]');
const menuItems = (): NodeListOf<HTMLElement> =>
  container.querySelectorAll('[data-testid=slash-menu] [role=option]');
const slashError = (): string | null =>
  container.querySelector('[data-testid=slash-error]')?.textContent ?? null;
const slashInfo = (): string | null =>
  container.querySelector('[data-testid=slash-info]')?.textContent ?? null;

async function mount(
  sessionId: string | null = 's-1',
  extra: Partial<Parameters<typeof Composer>[0]> = {},
): Promise<void> {
  await act(async () => {
    root.render(
      createElement(Composer, {
        streaming: false,
        onSubmit,
        onSteer,
        onCancel: () => {},
        onEnsureSession: async () => sessionId ?? 'auto',
        sessionId,
        models: [],
        selectedModel: null,
        ...extra,
      }),
    );
  });
}

async function typeText(text: string): Promise<void> {
  await act(async () => {
    const ta = textarea();
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      'value',
    )?.set;
    setter?.call(ta, text);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
  // #1200：菜单打开时才拉 registry——再冲一次微任务，让 mock 的 resolve 落到 DOM。
  await act(async () => {});
}

function press(key: string, opts: KeyboardEventInit = {}): boolean {
  let prevented = false;
  act(() => {
    textarea().dispatchEvent(
      new KeyboardEvent('keydown', {
        key,
        bubbles: true,
        cancelable: true,
        ...opts,
      }),
    );
  });
  return !prevented;
}

/** 真实 preventDefault 判定：上面 press 的 prevented 拿不到引用，改用可复用监听器。 */
function pressCaptured(key: string, opts: KeyboardEventInit = {}): { prevented: boolean } {
  const ta = textarea();
  const events: Event[] = [];
  const record = (e: Event) => {
    events.push(e);
  };
  ta.addEventListener('keydown', record);
  act(() => {
    ta.dispatchEvent(
      new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...opts }),
    );
  });
  ta.removeEventListener('keydown', record);
  // defaultPrevented 要在 dispatch 完整走完（React root 处理器跑过）之后读。
  const last = events[events.length - 1];
  return { prevented: last ? last.defaultPrevented : false };
}

beforeEach(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  onSubmit = vi.fn();
  onSteer = vi.fn();
  vi.mocked(compactSession).mockReset();
  vi.mocked(getTaskState).mockReset();
  vi.mocked(getSlashRegistry).mockReset();
  vi.mocked(getSlashRegistry).mockResolvedValue(REGISTRY);
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  await mount();
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('#1195 触发与渲染', () => {
  it('行首 / → 菜单开，内置命令与 Skills 两组齐全（单一注册表）', async () => {
    await typeText('/');
    expect(menu()).not.toBeNull();
    expect(menuItems().length).toBe(REGISTRY.length);
    expect(container.textContent).toContain('内置命令');
    expect(container.textContent).toContain('Skills');
  });

  it('skill 行展示 /name + argument-hint + 描述', async () => {
    await typeText('/');
    const item = container.querySelector('[data-slash-name="deep-research"]')!;
    expect(item.textContent).toContain('/deep-research');
    expect(item.textContent).toContain('主题…');
    expect(item.textContent).toContain('深度调研');
  });

  it('插件 skill 用 namespace:name 消歧', async () => {
    await typeText('/');
    const item = container.querySelector('[data-slash-name="acme:code-review"]')!;
    expect(item.textContent).toContain('/acme:code-review');
  });

  it('禁用 skill 由后端排除：注册表里没有就不渲染', async () => {
    vi.mocked(getSlashRegistry).mockResolvedValue(
      REGISTRY.filter((c) => c.name !== 'api-design'),
    );
    await mount();
    await typeText('/');
    expect(container.querySelector('[data-slash-name="api-design"]')).toBeNull();
  });

  it('空格后 / 触发', async () => {
    await typeText('帮我看看 /');
    expect(menu()).not.toBeNull();
  });

  it('全角 ／ 不触发', async () => {
    await typeText('／');
    expect(menu()).toBeNull();
  });

  it('词中间 / 不触发', async () => {
    await typeText('a/b');
    expect(menu()).toBeNull();
  });

  it('fuzzy 过滤：/pl → 只剩 plan', async () => {
    await typeText('/pl');
    expect(menuItems().length).toBe(1);
    expect(menuItems()[0].textContent).toContain('plan');
  });

  it('fuzzy 过滤 skill：/deep → deep-research', async () => {
    await typeText('/deep');
    expect(menuItems().length).toBe(1);
    expect(menuItems()[0].getAttribute('data-slash-name')).toBe('deep-research');
  });

  it('无命中空态：无匹配命令', async () => {
    await typeText('/zzz');
    expect(menu()).not.toBeNull();
    expect(menu()!.textContent).toContain('无匹配命令');
  });

  it('无会话（sessionId=null）菜单不触发', async () => {
    await mount(null);
    await typeText('/');
    expect(menu()).toBeNull();
  });
});

describe('#1195 键盘交互', () => {
  it('Esc 关闭菜单且不清空输入', async () => {
    await typeText('/pl');
    const before = textarea().value;
    press('Escape');
    expect(menu()).toBeNull();
    expect(textarea().value).toBe(before);
  });

  it('菜单开时 ArrowDown preventDefault 且移动选中项', async () => {
    await typeText('/');
    const first = menuItems()[0].getAttribute('aria-selected');
    const r = pressCaptured('ArrowDown');
    expect(r.prevented).toBe(true);
    const second = menuItems()[1].getAttribute('aria-selected');
    expect(first).toBe('true');
    expect(second).toBe('true');
  });

  it('Enter 执行选中命令并清空输入（不 onSubmit）', async () => {
    vi.mocked(compactSession).mockResolvedValue({} as never);
    await typeText('/compact');
    press('Enter');
    await act(async () => {});
    expect(compactSession).toHaveBeenCalledWith('s-1');
    expect(onSubmit).not.toHaveBeenCalled();
    expect(textarea().value).toBe('');
    expect(menu()).toBeNull();
  });

  it('唯一命中 /pl Enter 直接执行', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ id: 's-1', mode: 'plan' }), { status: 200 }));
    await typeText('/pl');
    expect(menuItems().length).toBe(1);
    press('Enter');
    await act(async () => {});
    const calls = fetchMock.mock.calls.filter((c: unknown[]) => String(c[0]).includes('/workflow-mode'));
    expect(calls).toHaveLength(1);
    const init = calls[0][1];
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({ mode: 'plan' });
  });

  it('菜单关（无 /）Enter 走原发送逻辑', async () => {
    await typeText('普通消息');
    press('Enter');
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});

describe('#1195 带参提交拦截', () => {
  it('/goal <文本> 拦截：本地执行，不发聊天', async () => {
    fetchMock.mockResolvedValue(new Response('{}', { status: 200 }));
    await typeText('/goal 每天投20份简历');
    press('Enter');
    await act(async () => {});
    const calls = fetchMock.mock.calls.filter((c: unknown[]) => String(c[0]).includes('/task/definition'));
    expect(calls).toHaveLength(1);
    const init = calls[0][1];
    expect(JSON.parse(init.body)).toEqual({ task_text: '每天投20份简历' });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(textarea().value).toBe('');
  });

  it('未知命令 /unknown foo 不拦截，当聊天发送', async () => {
    await typeText('/unknown foo');
    press('Enter');
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith('/unknown foo', false, [], 'session');
  });
});

describe('#1195 错误可见', () => {
  it('/compact 失败 → 错误面可见（含后端原因），不静默', async () => {
    vi.mocked(compactSession).mockRejectedValue(
      new (await import('../lib/api')).SessionError(409, '有 run 在途，稍后再压缩'),
    );
    await typeText('/compact');
    press('Enter');
    await act(async () => {});
    expect(slashError()).toContain('有 run 在途');
  });

  it('/goal 带参 422 → 后端 detail 透出', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: 'task_text 不能为空' }), { status: 422 }),
    );
    await typeText('/goal   ');
    // 带参拦截要求非空 args；直接用 /goal x 触发设置失败路径
    await typeText('/goal x');
    press('Enter');
    await act(async () => {});
    expect(slashError()).toContain('task_text');
  });
});

describe('#1195 /goal 无参查看', () => {
  it('已定义 → 中性 info 面展示当前目标', async () => {
    vi.mocked(getTaskState).mockResolvedValue(taskState('每天投20份简历'));
    await typeText('/goal');
    press('Enter');
    await act(async () => {});
    expect(getTaskState).toHaveBeenCalledWith('s-1');
    expect(slashInfo()).toContain('当前目标：每天投20份简历');
    expect(container.querySelector('[data-testid=slash-error]')).toBeNull();
  });

  it('未定义（TaskNotDefinedError）→ 中性 info 如实展示', async () => {
    const mod = await import('../lib/api');
    vi.mocked(getTaskState).mockRejectedValue(new mod.TaskNotDefinedError('未定义任务'));
    await typeText('/goal');
    press('Enter');
    await act(async () => {});
    expect(slashInfo()).toContain('未定义');
    expect(container.querySelector('[data-testid=slash-error]')).toBeNull();
  });
});

describe('#1195 /permission /model 拉起现有 UI', () => {
  it('/model 触发模型 picker 按钮点击', async () => {
    // Composer 无 models 目录时不渲染 picker——传 models 渲染后验证。
    await act(async () => {
      root.render(
        createElement(Composer, {
          streaming: false,
          onSubmit,
          onSteer: vi.fn(),
          onCancel: () => {},
          onEnsureSession: async () => 's-1',
          sessionId: 's-1',
          models: [
            {
              name: 'm1',
              display_name: 'M1',
              provider: 'p',
              capabilities: {},
            } as never,
          ],
          selectedModel: 'm1',
        }),
      );
    });
    const trigger = container.querySelector('button[aria-label="模型选择"]');
    expect(trigger).not.toBeNull();
    const clickSpy = vi.fn();
    const btn = trigger as HTMLButtonElement;
    btn.addEventListener('click', clickSpy);
    await typeText('/model');
    press('Enter');
    await act(async () => {});
    // Radix trigger 在 jsdom 下由 click 冒泡触发——验证我们确实 click 了它。
    expect(clickSpy).toHaveBeenCalled();
  });

  /** /permission 拉起断言体（独立的宿主 stub 作用域内执行，见下方 it）。 */
  const runPermissionPullUp = async (): Promise<void> => {
    await act(async () => {
      root.render(
        createElement(Composer, {
          streaming: false,
          onSubmit,
          onSteer,
          onCancel: () => {},
          onEnsureSession: async () => 's-1',
          sessionId: 's-1',
          permissionModes: [
            { id: 'default', display_name: '默认', description: '默认档' },
            { id: 'danger-full-access', display_name: '完全访问', description: '危险档' },
          ] as never,
          selectedPermissionMode: 'default',
        }),
      );
    });
    const trigger = container.querySelector('button[aria-label="权限模式"]');
    expect(trigger).not.toBeNull();
    const clickSpy = vi.fn();
    (trigger as HTMLButtonElement).addEventListener('click', clickSpy);
    await typeText('/permission');
    press('Enter');
    await act(async () => {});
    expect(clickSpy).toHaveBeenCalled();
    // 挂载时的 /api/attachments/limits 预拉是既有 effect，与命令无关——只看命令面。
    expect(
      fetchMock.mock.calls.filter((c: unknown[]) => !String(c[0]).includes('/api/attachments/limits')),
    ).toHaveLength(0);
    expect(onSubmit).not.toHaveBeenCalled();
  };

  it('/permission 触发权限 picker 按钮点击（B-S-2：拉起路径有测试钉住）', async () => {
    // OptionPicker 内部 cmdk 依赖 ResizeObserver 与 scrollIntoView（jsdom 都没有）——只 stub 本测试，收尾还原。
    const hadScrollIntoView = 'scrollIntoView' in Element.prototype;
    const originalScrollIntoView = (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe(): void {}
        unobserve(): void {}
        disconnect(): void {}
      },
    );
    (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView = vi.fn();
    try {
      await runPermissionPullUp();
    } finally {
      if (hadScrollIntoView) {
        (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView = originalScrollIntoView;
      } else {
        delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
      }
      vi.unstubAllGlobals();
    }
    // 恢复全局 fetch stub（上面 unstubAllGlobals 连带撤了 beforeEach 的 fetch stub）。
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });
});

describe('#1195 findings 修回 — 提交拦截汇合点（A-P1-1/A-P2-1/A-P2-3）', () => {
  it('Ctrl+Enter 对 /goal 带参同样拦截：本地执行，不 steer（A-P1-1）', async () => {
    fetchMock.mockResolvedValue(new Response('{}', { status: 200 }));
    await typeText('/goal 每天投20份简历');
    press('Enter', { ctrlKey: true });
    await act(async () => {});
    const calls = fetchMock.mock.calls.filter((c: unknown[]) => String(c[0]).includes('/task/definition'));
    expect(calls).toHaveLength(1);
    expect(JSON.parse((calls[0][1] as RequestInit).body as string)).toEqual({
      task_text: '每天投20份简历',
    });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onSteer).not.toHaveBeenCalled();
    expect(textarea().value).toBe('');
  });

  it('发送按钮点击对 /goal 带参同样拦截（A-P1-1）', async () => {
    fetchMock.mockResolvedValue(new Response('{}', { status: 200 }));
    await typeText('/goal 另一个目标');
    await act(async () => {
      (container.querySelector('button[aria-label="发送"]') as HTMLButtonElement).click();
    });
    await act(async () => {});
    const calls = fetchMock.mock.calls.filter((c: unknown[]) => String(c[0]).includes('/task/definition'));
    expect(calls).toHaveLength(1);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(textarea().value).toBe('');
  });

  it('尾随空格 /goal␣ + Enter 落查看分支（A-P2-1）', async () => {
    vi.mocked(getTaskState).mockResolvedValue(taskState('已有目标'));
    await typeText('/goal ');
    press('Enter');
    await act(async () => {});
    expect(getTaskState).toHaveBeenCalledWith('s-1');
    expect(
      fetchMock.mock.calls.filter((c: unknown[]) => String(c[0]).includes('/task/definition')),
    ).toHaveLength(0);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(slashInfo()).toContain('当前目标：已有目标');
  });

  it('locked（审批待决）时命令不执行、也不当聊天发（A-P2-3）', async () => {
    await mount('s-1', { approvalPending: true });
    await typeText('/plan');
    press('Enter');
    await act(async () => {});
    // 挂载时的 /api/attachments/limits 预拉是既有 effect，与命令无关——只看命令/发送面。
    const cmdCalls = fetchMock.mock.calls.filter(
      (c: unknown[]) => !String(c[0]).includes('/api/attachments/limits'),
    );
    expect(cmdCalls).toHaveLength(0);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onSteer).not.toHaveBeenCalled();
  });
});

describe('#1195 findings 修回 — 菜单与反馈面', () => {
  it('菜单 Enter 只切除命令 token，保留前缀（B-S-3）', async () => {
    vi.mocked(compactSession).mockResolvedValue({} as never);
    await typeText('hello /compact');
    expect(menu()).not.toBeNull();
    press('Enter');
    await act(async () => {});
    expect(compactSession).toHaveBeenCalledWith('s-1');
    expect(textarea().value).toBe('hello');
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('/goal 查看结果走中性 info 面（role=status），不是红色 alert（B-S-4/A-P3-2）', async () => {
    vi.mocked(getTaskState).mockResolvedValue(taskState('每天投20份简历'));
    await typeText('/goal');
    press('Enter');
    await act(async () => {});
    const info = container.querySelector('[data-testid=slash-info]');
    expect(info?.getAttribute('role')).toBe('status');
    expect(info?.textContent).toContain('当前目标：每天投20份简历');
    expect(container.querySelector('[data-testid=slash-error]')).toBeNull();
  });

  it('/goal 二次设置 409 → detail + 可操作引导（A-P2-2）', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: '任务已定义（task/defined 全流至多一条）' }), { status: 409 }),
    );
    await typeText('/goal 换个目标');
    press('Enter');
    await act(async () => {});
    expect(slashError()).toContain('任务已定义');
    expect(slashError()).toContain('/goal（无参数）可查看当前目标');
  });

  it('无会话时带参命令给可见反馈且不清输入（A-P3-1）', async () => {
    await mount(null);
    await typeText('/goal 每天投20份简历');
    press('Enter');
    await act(async () => {});
    expect(slashError()).toContain('没有会话');
    expect(textarea().value).toBe('/goal 每天投20份简历');
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('outside-click 关闭菜单（A-P3-3）', async () => {
    await typeText('/pl');
    expect(menu()).not.toBeNull();
    await act(async () => {
      document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    });
    expect(menu()).toBeNull();
    // 输入原样保留（关闭 ≠ 清输入）。
    expect(textarea().value).toBe('/pl');
  });

  it('菜单开时 textarea 带 ARIA 关联（S-7）：expanded/controls/activedescendant', async () => {
    await typeText('/pl');
    const ta = textarea();
    expect(ta.getAttribute('aria-expanded')).toBe('true');
    const menuId = ta.getAttribute('aria-controls');
    expect(menuId).toBeTruthy();
    expect(document.getElementById(menuId!)).not.toBeNull();
    expect(ta.getAttribute('aria-activedescendant')).toBe('slash-opt-plan');
  });
});

describe('#1195 findings 修回 — /compact 三态（A-P1-3，#1197 契约）', () => {
  it('压缩中：spinner 面可见（正在压缩上下文…），在途重复触发不重复请求', async () => {
    let resolveCompact!: (v: SessionContextCompacted) => void;
    vi.mocked(compactSession).mockImplementation(
      () => new Promise((res) => { resolveCompact = res; }),
    );
    await typeText('/compact');
    press('Enter');
    await act(async () => {});
    const compacting = container.querySelector('[data-testid=slash-compacting]');
    expect(compacting?.getAttribute('role')).toBe('status');
    expect(compacting?.textContent).toContain('正在压缩上下文');
    press('Enter');
    await act(async () => {});
    expect(compactSession).toHaveBeenCalledTimes(1);
    resolveCompact({
      bracket_id: 'b1', source_seq_start: 0, source_seq_end: 9,
      tokens_before: 100000, tokens_after: 40000, compacted_turn_count: 3, summary_model: 'm',
    });
    await act(async () => {});
    expect(container.querySelector('[data-testid=slash-compacting]')).toBeNull();
    expect(slashInfo()).toContain('压缩完成：100,000 → 40,000 tok');
  });

  it('被拒（compacted_turn_count=0 零写入）：中性信息「没有可压缩的早期内容」，不是错误', async () => {
    vi.mocked(compactSession).mockResolvedValue({
      bracket_id: null, source_seq_start: null, source_seq_end: null,
      tokens_before: 500, tokens_after: 500, compacted_turn_count: 0, summary_model: null,
    } as never);
    await typeText('/compact');
    press('Enter');
    await act(async () => {});
    expect(slashInfo()).toBe('没有可压缩的早期内容');
    expect(container.querySelector('[data-testid=slash-error]')).toBeNull();
  });

  it('压缩失败错误面 8s 自动消退（1195-FIX2 A-P2：不常驻到下一次会话结束）', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(compactSession).mockRejectedValue(
        new (await import('../lib/api')).SessionError(409, '有 run 在途，稍后再压缩'),
      );
      await typeText('/compact');
      press('Enter');
      await act(async () => {});
      expect(slashError()).toContain('有 run 在途');
      await act(async () => {
        await vi.advanceTimersByTimeAsync(8000);
      });
      expect(slashError()).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('#1195 复审修回（1195-FIX2）', () => {
  it('streaming 中 /permission：disabled 的 pill 不被程序化点击，给可见反馈（A-P2）', async () => {
    await mount('s-1', {
      streaming: true,
      permissionModes: [
        { id: 'default', display_name: '默认', description: '默认档' },
      ] as never,
      selectedPermissionMode: 'default',
    });
    const trigger = container.querySelector('button[aria-label="权限模式"]') as HTMLButtonElement;
    expect(trigger).not.toBeNull();
    // 前提确认：pill 此刻确实 disabled（locked||streaming 的 streaming 半边）
    expect(trigger.disabled).toBe(true);
    const clickSpy = vi.fn();
    trigger.addEventListener('click', clickSpy);
    await typeText('/permission');
    press('Enter');
    await act(async () => {});
    // 静默失效被消灭：click 没发生（disabled 按钮）+ 与 pill disabledHint 同源的可见反馈
    expect(clickSpy).not.toHaveBeenCalled();
    expect(slashInfo()).toContain('本轮进行中');
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
