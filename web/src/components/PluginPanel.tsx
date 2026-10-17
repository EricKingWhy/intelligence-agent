/** PluginPanel — 「插件」管理窗口（#1200；效果图 docs/plans/plugin-manager/plugin.html）。
 *
 * 左列表 = MCP SERVERS / SKILLS 两组（状态点 + 名字 + meta + 来源标签），
 * 右详情 = 状态 pill + 操作 + 工具 chips + 配置摘要 + 行为开关。
 *
 * 真相来源：`GET /api/mcp/servers`（config ∪ runtime）与 `GET /api/skills`（含禁用项）。
 * 写端点只落 `plugin-settings.json`（运行时选择态，裁决 C1）；**下次装配生效**
 * （裁决 D1）——每次写成功后重新拉取，并在界面上明示「重启后生效」，不做热重载。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Puzzle, RefreshCw, X } from 'lucide-react';
import {
  getMcpServerTools,
  getMcpServers,
  getSkills,
  reconnectMcpServer,
  setMcpRequireConfirm,
  setMcpServerEnabled,
  setMcpToolEnabled,
  setSkillEnabled,
  type McpServerEntry,
  type McpServerToolEntry,
  type SkillEntry,
} from '../lib/api';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const ORIGIN_LABELS: Record<SkillEntry['origin'], string> = {
  personal: 'Personal',
  project: 'Project',
  plugin: 'Plugin',
  oauth: 'OAuth',
};

const RESTART_NOTICE = '已保存；重启后生效。';

export function PluginPanel({ open, onOpenChange }: Props) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        {open && <PluginPanelBody />}
      </Dialog.Portal>
    </Dialog.Root>
  );
}

type Selection = { kind: 'server' | 'skill'; name: string };

function PluginPanelBody() {
  const [servers, setServers] = useState<McpServerEntry[]>([]);
  const [skills, setSkills] = useState<SkillEntry[]>([]);
  const [loadErrors, setLoadErrors] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState('');
  const [selection, setSelection] = useState<Selection | null>(null);
  const [tools, setTools] = useState<McpServerToolEntry[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [mcp, catalog] = await Promise.all([
        getMcpServers(),
        // skills capability 未装配 → 503：这是"没启用"，不是窗口坏了。
        getSkills().catch((e: unknown) => ({
          skills: [] as SkillEntry[],
          errors: [e instanceof Error ? e.message : 'skill 目录不可用'],
          conflicts: [] as string[],
        })),
      ]);
      setServers(mcp.servers);
      setSkills(catalog.skills);
      setLoadErrors([...mcp.errors, ...catalog.errors, ...catalog.conflicts]);
      setSelection((current) => {
        if (current) {
          const stillThere =
            current.kind === 'server'
              ? mcp.servers.some((s) => s.name === current.name)
              : catalog.skills.some((s) => s.name === current.name);
          if (stillThere) return current;
        }
        const firstServer = mcp.servers[0];
        if (firstServer) return { kind: 'server', name: firstServer.name };
        const firstSkill = catalog.skills[0];
        return firstSkill ? { kind: 'skill', name: firstSkill.name } : null;
      });
    } catch (e) {
      setError(`加载插件列表失败：${(e as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const selectedServer =
    selection?.kind === 'server'
      ? servers.find((s) => s.name === selection.name) ?? null
      : null;
  const selectedSkill =
    selection?.kind === 'skill'
      ? skills.find((s) => s.name === selection.name) ?? null
      : null;

  // 工具清单按选中 server 现拉（含被工具级禁用的条目）。
  const serverName = selectedServer?.name ?? null;
  useEffect(() => {
    if (serverName === null) {
      setTools(null);
      return;
    }
    let cancelled = false;
    setTools(null);
    getMcpServerTools(serverName)
      .then((items) => {
        if (!cancelled) setTools(items);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(`加载工具清单失败：${(e as Error).message}`);
      });
    return () => {
      cancelled = true;
    };
  }, [serverName]);

  const run = async (key: string, action: () => Promise<void>, done = RESTART_NOTICE) => {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await action();
      setNotice(done);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const needle = query.trim().toLowerCase();
  const visibleServers = useMemo(
    () => (needle ? servers.filter((s) => s.name.toLowerCase().includes(needle)) : servers),
    [servers, needle],
  );
  const visibleSkills = useMemo(
    () => (needle ? skills.filter((s) => s.name.toLowerCase().includes(needle)) : skills),
    [skills, needle],
  );

  return (
    <Dialog.Content className="project-dialog plugin-dialog" data-testid="plugin-panel">
      <div className="project-dialog-head">
        <Dialog.Title className="project-dialog-title">
          <Puzzle size={14} aria-hidden="true" /> 插件
        </Dialog.Title>
        <Dialog.Close asChild>
          <button className="icon-btn project-dialog-close" aria-label="关闭">
            <X size={14} />
          </button>
        </Dialog.Close>
      </div>
      <Dialog.Description className="project-dialog-desc">
        MCP Servers 与 Skills 的启用 / 禁用。禁用不等于删除；改动在下次装配（重启）后生效。
      </Dialog.Description>

      <div className="plugin-body">
        <div className="plugin-side">
          <input
            className="plugin-search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索插件…"
            aria-label="搜索插件"
          />
          {/* 列表语义：这里是一组**按钮**，不是 list/listitem——把 listitem 压在
              button 上会顶掉 button 角色（读屏只剩"列表项"，不再说可点击）。 */}
          <div className="plugin-list">
            <div className="plugin-group">MCP SERVERS · {visibleServers.length}</div>
            {visibleServers.map((server) => {
              const selected =
                selection?.kind === 'server' && selection.name === server.name;
              const dot = !server.enabled
                ? 'gray'
                : server.needs_auth
                  ? 'amber'
                  : server.connected
                    ? 'green'
                    : 'gray';
              const meta = !server.enabled
                ? '已禁用'
                : server.needs_auth
                  ? '需要认证'
                  : server.connected
                    ? `已连接 · ${server.tool_count} 个工具`
                    : `未连接 · ${server.tool_count} 个工具`;
              return (
                <button
                  key={server.name}
                  type="button"
                  data-testid={`plugin-row-${server.name}`}
                  className={`plugin-row${selected ? ' selected' : ''}`}
                  onClick={() => setSelection({ kind: 'server', name: server.name })}
                >
                  <span className={`dot ${dot}`} aria-hidden="true" />
                  <span className="plugin-row-text">
                    <span className="plugin-name">{server.name}</span>
                    <span className="plugin-meta">{meta}</span>
                  </span>
                  {server.needs_auth && <span className="plugin-tag">OAuth</span>}
                </button>
              );
            })}
            <div className="plugin-group">SKILLS · {visibleSkills.length}</div>
            {visibleSkills.map((skill) => {
              const selected =
                selection?.kind === 'skill' && selection.name === skill.name;
              return (
                <button
                  key={skill.name}
                  type="button"
                  data-testid={`plugin-row-${skill.name}`}
                  className={`plugin-row${selected ? ' selected' : ''}`}
                  onClick={() => setSelection({ kind: 'skill', name: skill.name })}
                >
                  <span
                    className={`dot ${skill.enabled ? 'green' : 'gray'}`}
                    aria-hidden="true"
                  />
                  <span className="plugin-row-text">
                    <span className="plugin-name">{skill.name}</span>
                    <span className="plugin-meta">
                      {skill.enabled ? skill.description : '已禁用'}
                    </span>
                  </span>
                  <span className="plugin-tag">{ORIGIN_LABELS[skill.origin]}</span>
                </button>
              );
            })}
            {!loading && visibleServers.length === 0 && visibleSkills.length === 0 && (
              <div className="plugin-empty" role="status">
                {needle ? '没有匹配的插件' : '没有已配置的 MCP server 或 skill'}
              </div>
            )}
          </div>
        </div>

        <div className="plugin-detail" data-testid="plugin-detail">
          {selectedServer && (
            <ServerDetail
              server={selectedServer}
              tools={tools}
              busy={busy}
              onReconnect={() =>
                run(
                  `reconnect:${selectedServer.name}`,
                  async () => {
                    await reconnectMcpServer(selectedServer.name);
                  },
                  '已重新连接。',
                )
              }
              onToggleEnabled={(enabled) =>
                run(`server:${selectedServer.name}`, () =>
                  setMcpServerEnabled(selectedServer.name, enabled),
                )
              }
              onToggleRequireConfirm={(value) =>
                run(`confirm:${selectedServer.name}`, () =>
                  setMcpRequireConfirm(selectedServer.name, value),
                )
              }
              onToggleTool={(tool, enabled) =>
                run(`tool:${selectedServer.name}:${tool}`, () =>
                  setMcpToolEnabled(selectedServer.name, tool, enabled),
                )
              }
            />
          )}
          {selectedSkill && (
            <SkillDetail
              skill={selectedSkill}
              busy={busy === `skill:${selectedSkill.name}`}
              onToggleEnabled={(enabled) =>
                run(`skill:${selectedSkill.name}`, () =>
                  setSkillEnabled(selectedSkill.name, enabled),
                )
              }
            />
          )}
          {!selectedServer && !selectedSkill && (
            <div className="plugin-empty" role="status">
              选择左侧的 MCP server 或 skill 查看详情。
            </div>
          )}
        </div>
      </div>

      {error && (
        <div className="approve-policy-error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="approve-policy-notice" role="status">
          {notice}
        </div>
      )}
      {loadErrors.length > 0 && (
        <div className="plugin-errors" role="status">
          {loadErrors.map((message) => (
            <div key={message}>{message}</div>
          ))}
        </div>
      )}

      <div className="project-dialog-actions">
        <button
          type="button"
          className="approve-policy-refresh"
          onClick={() => void load()}
          aria-label="刷新插件列表"
        >
          <RefreshCw size={14} /> 刷新
        </button>
        <Dialog.Close asChild>
          <button type="button" className="approve-policy-quiet">
            关闭
          </button>
        </Dialog.Close>
      </div>
    </Dialog.Content>
  );
}

function ServerDetail({
  server,
  tools,
  busy,
  onReconnect,
  onToggleEnabled,
  onToggleRequireConfirm,
  onToggleTool,
}: {
  server: McpServerEntry;
  tools: McpServerToolEntry[] | null;
  busy: string | null;
  onReconnect: () => void;
  onToggleEnabled: (enabled: boolean) => void;
  onToggleRequireConfirm: (value: boolean) => void;
  onToggleTool: (tool: string, enabled: boolean) => void;
}) {
  const status = !server.enabled
    ? { label: '已禁用', tone: 'gray' }
    : server.needs_auth
      ? { label: '需要认证', tone: 'amber' }
      : server.connected
        ? { label: '已连接', tone: 'green' }
        : { label: '未连接', tone: 'amber' };
  const enabledBusy = busy === `server:${server.name}`;
  const confirmBusy = busy === `confirm:${server.name}`;
  const reconnectBusy = busy === `reconnect:${server.name}`;

  return (
    <>
      <div className="pd-head">
        <span className="pd-name">{server.name}</span>
        <span className={`pill ${status.tone}`}>{status.label}</span>
        <div className="pd-actions">
          <button
            type="button"
            className="plugin-btn"
            disabled={reconnectBusy || !server.enabled}
            onClick={onReconnect}
          >
            {reconnectBusy ? '重连中…' : '重新连接'}
          </button>
          <button
            type="button"
            className="plugin-btn"
            disabled={enabledBusy}
            onClick={() => onToggleEnabled(!server.enabled)}
          >
            {server.enabled ? '禁用' : '启用'}
          </button>
        </div>
      </div>

      <div className="pd-sec">
        <div className="pd-sec-title">
          工具（{tools?.length ?? server.tool_count}）— 由模型自动调用，不进 / 菜单
        </div>
        {tools === null ? (
          <div className="plugin-meta">正在加载工具清单…</div>
        ) : tools.length === 0 ? (
          <div className="plugin-meta">该 server 没有发现工具。</div>
        ) : (
          <div className="tool-grid">
            {tools.map((tool) => (
              <button
                key={tool.name}
                type="button"
                className={`tool-chip${tool.enabled ? '' : ' off'}`}
                aria-pressed={tool.enabled}
                title={
                  tool.description
                    ? `${tool.description}（点击${tool.enabled ? '禁用' : '启用'}）`
                    : `点击${tool.enabled ? '禁用' : '启用'}`
                }
                disabled={busy === `tool:${server.name}:${tool.name}`}
                onClick={() => onToggleTool(tool.name, !tool.enabled)}
              >
                {tool.name}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="pd-sec">
        <div className="pd-sec-title">配置</div>
        <div className="pd-config">
          transport <code>{server.transport || '—'}</code>
          {server.command && (
            <>
              {' · '}command <code>{server.command}</code>
            </>
          )}
          {server.url && (
            <>
              {' · '}url <code>{server.url}</code>
            </>
          )}
        </div>
      </div>

      <div className="pd-sec">
        <div className="pd-sec-title">行为</div>
        <label className="switch-row">
          <input
            type="checkbox"
            className="plugin-switch"
            checked={server.enabled}
            disabled={enabledBusy}
            onChange={(e) => onToggleEnabled(e.target.checked)}
          />
          启用此 server
        </label>
        <label className="switch-row">
          <input
            type="checkbox"
            className="plugin-switch"
            checked={server.require_confirm}
            disabled={confirmBusy}
            onChange={(e) => onToggleRequireConfirm(e.target.checked)}
          />
          调用工具前需要我确认
        </label>
      </div>
    </>
  );
}

function SkillDetail({
  skill,
  busy,
  onToggleEnabled,
}: {
  skill: SkillEntry;
  busy: boolean;
  onToggleEnabled: (enabled: boolean) => void;
}) {
  return (
    <>
      <div className="pd-head">
        <span className="pd-name">{skill.name}</span>
        <span className={`pill ${skill.enabled ? 'green' : 'gray'}`}>
          {skill.enabled ? '已启用' : '已禁用'}
        </span>
        <div className="pd-actions">
          <button
            type="button"
            className="plugin-btn"
            disabled={busy}
            onClick={() => onToggleEnabled(!skill.enabled)}
          >
            {skill.enabled ? '禁用' : '启用'}
          </button>
        </div>
      </div>
      <div className="pd-sec">
        <div className="pd-sec-title">说明</div>
        <div className="pd-config">{skill.description || '（无描述）'}</div>
        {skill.when_to_use && (
          <div className="plugin-meta">何时用：{skill.when_to_use}</div>
        )}
      </div>
      <div className="pd-sec">
        <div className="pd-sec-title">来源</div>
        <div className="pd-config">
          {ORIGIN_LABELS[skill.origin]} · {skill.scope === 'global' ? '全局' : '工作区'}
          {skill.plugin && (
            <>
              {' · '}插件 <code>{skill.plugin}</code>
            </>
          )}
        </div>
        <div className="plugin-meta plugin-source" title={skill.source}>
          {skill.source}
        </div>
      </div>
      <div className="pd-sec">
        <div className="pd-sec-title">行为</div>
        <label className="switch-row">
          <input
            type="checkbox"
            className="plugin-switch"
            checked={skill.enabled}
            disabled={busy}
            onChange={(e) => onToggleEnabled(e.target.checked)}
          />
          启用此 skill
        </label>
      </div>
    </>
  );
}
