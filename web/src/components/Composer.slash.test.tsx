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
  };
});
vi.mock('../lib/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/auth')>();
  return { ...actual, getToken: vi.fn(() => null) };
});

import { compactSession, getTaskState, type TaskState } from '../lib/api';
import { Composer } from './Composer';

type SubmitArgs = [task: string, rememberAsProceduralRule: boolean, attachmentIds?: string[], scope?: 'session' | 'project' | 'global'];

let container: HTMLDivElement;
let root: Root;
let onSubmit: Mock<(...args: SubmitArgs) => void>;
let fetchMock: Mock;

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

async function mount(sessionId: string | null = 's-1'): Promise<void> {
  await act(async () => {
    root.render(
      createElement(Composer, {
        streaming: false,
        onSubmit,
        onSteer: vi.fn(),
        onCancel: () => {},
        onEnsureSession: async () => sessionId ?? 'auto',
        sessionId,
        models: [],
        selectedModel: null,
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
  vi.mocked(compactSession).mockReset();
  vi.mocked(getTaskState).mockReset();
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
  it('行首 / → 菜单开，5 条命令按分组齐全', async () => {
    await typeText('/');
    expect(menu()).not.toBeNull();
    expect(menuItems().length).toBe(5);
    expect(container.textContent).toContain('添加');
    expect(container.textContent).toContain('指令');
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
    expect(menuItems()[0].textContent).toContain('计划');
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
  it('已定义 → 展示当前目标', async () => {
    vi.mocked(getTaskState).mockResolvedValue(taskState('每天投20份简历'));
    await typeText('/goal');
    press('Enter');
    await act(async () => {});
    expect(getTaskState).toHaveBeenCalledWith('s-1');
    expect(slashError()).toContain('当前目标：每天投20份简历');
  });

  it('未定义（TaskNotDefinedError）→ 如实展示', async () => {
    const mod = await import('../lib/api');
    vi.mocked(getTaskState).mockRejectedValue(new mod.TaskNotDefinedError('未定义任务'));
    await typeText('/goal');
    press('Enter');
    await act(async () => {});
    expect(slashError()).toContain('未定义');
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
});
