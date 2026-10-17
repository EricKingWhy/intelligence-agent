// @vitest-environment jsdom
/** #1200 — 插件管理窗口（jsdom 行为测试）。
 *
 *  判据（票面 §4/§6 Phase 1+2）：
 *   1. 左列表按 MCP SERVERS / SKILLS 分组，行上有状态点 / meta / 来源标签；
 *   2. 选中 server → 右详情显示状态 pill、工具 chips（禁用项划线）、配置摘要、行为开关；
 *   3. 禁用 / 启用、工具级启停、require-confirm、skill 启停都打到对应端点；
 *   4. 写成功后明示「重启后生效」（裁决 D1，不假装热重载）；
 *   5. 失败原因可见（role=alert），不静默。
 */
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    getMcpServers: vi.fn(),
    getMcpServerTools: vi.fn(),
    getSkills: vi.fn(),
    setMcpServerEnabled: vi.fn(),
    setMcpToolEnabled: vi.fn(),
    setMcpRequireConfirm: vi.fn(),
    reconnectMcpServer: vi.fn(),
    setSkillEnabled: vi.fn(),
  };
});

import {
  getMcpServers,
  getMcpServerTools,
  getSkills,
  reconnectMcpServer,
  setMcpRequireConfirm,
  setMcpServerEnabled,
  setMcpToolEnabled,
  setSkillEnabled,
  type McpServerEntry,
  type SkillEntry,
} from '../lib/api';
import { PluginPanel } from './PluginPanel';

const SERVERS: McpServerEntry[] = [
  {
    name: 'playwright',
    connected: true,
    enabled: true,
    needs_auth: false,
    tool_count: 2,
    require_confirm: false,
    transport: 'stdio',
    command: 'npx @playwright/mcp',
    url: '',
  },
  {
    name: 'github',
    connected: false,
    enabled: true,
    needs_auth: true,
    tool_count: 0,
    require_confirm: true,
    transport: 'http',
    command: '',
    url: 'https://example.test/mcp',
  },
  {
    name: 'filesystem',
    connected: false,
    enabled: false,
    needs_auth: false,
    tool_count: 0,
    require_confirm: false,
    transport: 'stdio',
    command: 'npx filesystem',
    url: '',
  },
];

const SKILLS: SkillEntry[] = [
  {
    name: 'api-design',
    description: 'REST API 设计审查',
    when_to_use: '',
    source: '/ws/skills/api-design/SKILL.md',
    scope: 'workspace',
    origin: 'project',
    enabled: true,
  },
  {
    name: 'code-review',
    description: '双轴代码审查',
    when_to_use: '',
    source: '/ws/plugins/acme/skills/code-review/SKILL.md',
    scope: 'workspace',
    origin: 'plugin',
    plugin: 'acme',
    enabled: false,
  },
];

let container: HTMLDivElement;
let root: Root;
let onOpenChange: Mock<(open: boolean) => void>;

// Radix Dialog 走 Portal 渲染到 document.body（与 ApprovalModal 测试同款）：
// 查询面必须是 body，而不是挂载宿主容器。
const row = (name: string): HTMLElement | null =>
  document.body.querySelector(`[data-testid="plugin-row-${name}"]`);
const detail = (): HTMLElement => document.body.querySelector('[data-testid="plugin-detail"]')!;
const alert = (): string | null =>
  document.body.querySelector('[role="alert"]')?.textContent ?? null;
const status = (): string | null =>
  document.body.querySelector('[role="status"]')?.textContent ?? null;

async function mount(open = true): Promise<void> {
  await act(async () => {
    root.render(createElement(PluginPanel, { open, onOpenChange }));
  });
  await act(async () => {});
}

async function click(el: Element | null): Promise<void> {
  expect(el).not.toBeNull();
  await act(async () => {
    (el as HTMLElement).dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  await act(async () => {});
}

beforeEach(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  onOpenChange = vi.fn();
  vi.mocked(getMcpServers).mockReset();
  vi.mocked(getMcpServerTools).mockReset();
  vi.mocked(getSkills).mockReset();
  vi.mocked(setMcpServerEnabled).mockReset();
  vi.mocked(setMcpToolEnabled).mockReset();
  vi.mocked(setMcpRequireConfirm).mockReset();
  vi.mocked(reconnectMcpServer).mockReset();
  vi.mocked(setSkillEnabled).mockReset();
  vi.mocked(getMcpServers).mockResolvedValue({ servers: SERVERS, errors: [] });
  vi.mocked(getSkills).mockResolvedValue({ skills: SKILLS, errors: [], conflicts: [] });
  vi.mocked(getMcpServerTools).mockResolvedValue([
    { name: 'browser_navigate', description: '打开页面', enabled: true },
    { name: 'browser_upload', description: '', enabled: false },
  ]);
  await mount();
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
  vi.clearAllMocks();
});

describe('#1200 Phase 1 只读渲染', () => {
  it('两组分组标题与行齐全', () => {
    expect(document.body.textContent).toContain('MCP SERVERS · 3');
    expect(document.body.textContent).toContain('SKILLS · 2');
    expect(row('playwright')).not.toBeNull();
    expect(row('api-design')).not.toBeNull();
  });

  it('行 meta：已连接工具数 / 需要认证 / 已禁用', () => {
    expect(row('playwright')!.textContent).toContain('已连接 · 2 个工具');
    expect(row('github')!.textContent).toContain('需要认证');
    expect(row('github')!.textContent).toContain('OAuth');
    expect(row('filesystem')!.textContent).toContain('已禁用');
  });

  it('skill 行带来源标签；禁用 skill 标「已禁用」', () => {
    expect(row('api-design')!.textContent).toContain('Project');
    expect(row('code-review')!.textContent).toContain('Plugin');
    expect(row('code-review')!.textContent).toContain('已禁用');
  });

  it('默认选中首个 server，详情含 pill / 工具 chips / 配置摘要', () => {
    const text = detail().textContent!;
    expect(text).toContain('playwright');
    expect(text).toContain('已连接');
    expect(text).toContain('browser_navigate');
    expect(text).toContain('browser_upload');
    expect(text).toContain('stdio');
    expect(text).toContain('npx @playwright/mcp');
    expect(text).toContain('不进 / 菜单');
  });

  it('工具数标题以实际工具清单为准（列表 tool_count 只是列表页摘要）', async () => {
    // 发现结果为空但列表摘要说 2：标题必须显示 0，不能拿摘要冒充详情。
    vi.mocked(getMcpServerTools).mockResolvedValue([]);
    await act(async () => {
      root.unmount();
    });
    root = createRoot(container);
    await mount();
    expect(detail().textContent).toContain('工具（0）');
    expect(detail().textContent).not.toContain('工具（2）');
  });

  it('被禁用的工具 chip 带 off 类（划线）', () => {
    const chip = [...detail().querySelectorAll('.tool-chip')].find(
      (el) => el.textContent === 'browser_upload',
    )!;
    expect(chip.className).toContain('off');
    expect(chip.getAttribute('aria-pressed')).toBe('false');
  });

  it('加载失败原因可见（errors 列表）', async () => {
    vi.mocked(getMcpServers).mockResolvedValue({ servers: [], errors: ['server 装配失败'] });
    vi.mocked(getSkills).mockResolvedValue({ skills: [], errors: [], conflicts: [] });
    // 面板体只在 open=true 时挂载；同 props 重渲染不会重跑 effect ⇒ 必须真正重挂。
    await act(async () => {
      root.unmount();
    });
    root = createRoot(container);
    await mount();
    expect(document.body.textContent).toContain('server 装配失败');
  });
});

describe('#1200 Phase 2 管理动作', () => {
  it('禁用 server → 调端点并提示重启后生效', async () => {
    await click([...detail().querySelectorAll('button')].find((b) => b.textContent === '禁用') ?? null);
    expect(setMcpServerEnabled).toHaveBeenCalledWith('playwright', false);
    expect(status()).toContain('重启后生效');
  });

  it('工具 chip 点击 → 工具级启用', async () => {
    const chip = [...detail().querySelectorAll('.tool-chip')].find(
      (el) => el.textContent === 'browser_upload',
    )!;
    await click(chip);
    expect(setMcpToolEnabled).toHaveBeenCalledWith('playwright', 'browser_upload', true);
  });

  it('require-confirm 开关 → 对应端点', async () => {
    const switches = detail().querySelectorAll('input[type=checkbox]');
    const confirmSwitch = switches[1] as HTMLInputElement;
    await act(async () => {
      confirmSwitch.click();
    });
    expect(setMcpRequireConfirm).toHaveBeenCalledWith('playwright', true);
  });

  it('重新连接 → 调端点', async () => {
    await click(
      [...detail().querySelectorAll('button')].find((b) => b.textContent === '重新连接') ?? null,
    );
    expect(reconnectMcpServer).toHaveBeenCalledWith('playwright');
  });

  it('skill 选中后禁用 → 调端点', async () => {
    await click(row('api-design'));
    await click([...detail().querySelectorAll('button')].find((b) => b.textContent === '禁用') ?? null);
    expect(setSkillEnabled).toHaveBeenCalledWith('api-design', false);
  });

  it('写失败原因可见（role=alert），不静默', async () => {
    vi.mocked(setMcpServerEnabled).mockRejectedValue(new Error('plugin-settings.json 不可写'));
    await click([...detail().querySelectorAll('button')].find((b) => b.textContent === '禁用') ?? null);
    expect(alert()).toContain('plugin-settings.json 不可写');
  });

  it('搜索过滤列表', async () => {
    const input = document.body.querySelector('input[aria-label="搜索插件"]') as HTMLInputElement;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,
        'value',
      )?.set;
      setter?.call(input, 'git');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(row('github')).not.toBeNull();
    expect(row('playwright')).toBeNull();
  });
});
