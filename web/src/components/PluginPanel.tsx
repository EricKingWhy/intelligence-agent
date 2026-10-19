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
  disconnectMcpServer,
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

/* 来源标签：数据源是 `origin` 枚举本身（票面 §5「直接引用 origin 枚举值渲染」），
   文案走中文——PRODUCT.md Brand Commitments「界面语言以中文为主」，同面板其余文案
   （已连接 / 需要认证 / 工作区）全中文；效果图里的英文是设计稿占位。 */
const ORIGIN_LABELS: Record<SkillEntry['origin'], string> = {
  personal: '个人',
  project: '项目',
  plugin: '插件',
  oauth: 'OAuth 授权',
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
  // 抽成回调：工具开关写完后要重拉这份清单——`load()` 只刷新 server/skill 列表，
  // 不碰 chips，否则界面上按下的状态永远停在旧值（写成功但看起来没生效）。
  const refreshTools = useCallback(async (name: string) => {
    try {
      setTools(await getMcpServerTools(name));
    } catch (e) {
      setError(`加载工具清单失败：${(e as Error).message}`);
    }
  }, []);
  useEffect(() => {
    if (serverName === null) {
      setTools(null);
      return;
    }
    setTools(null);
    void refreshTools(serverName);
  }, [serverName, refreshTools]);

  /** `done=null`：动作自己用 `setNotice` 写更精确的结果（重连成功/失败两态）。 */
  const run = async (
    key: string,
    action: () => Promise<void>,
    done: string | null = RESTART_NOTICE,
  ) => {
    setBusy(key);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (done !== null) setNotice(done);
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
            {/* #1325 S7：首帧数据未返回前不渲染分组计数——「MCP SERVERS · 0」
                会被读成「确实没有插件」，与加载态是两种信号。加载指示沿用
                `.ctx-usage-empty` 的空态排版 + `.ctx-usage-spinner`（同一窗口族）。 */}
            {loading && (
              <div className="plugin-empty plugin-loading" role="status">
                <span className="ctx-usage-spinner" aria-hidden="true" />
                加载中…
              </div>
            )}
            {!loading && (
              <div className="plugin-group">MCP SERVERS · {visibleServers.length}</div>
            )}
            {visibleServers.map((server) => {
              const selected =
                selection?.kind === 'server' && selection.name === server.name;
              const dot = !server.config_enabled
                ? 'gray'
                : !server.enabled
                ? 'gray'
                : server.needs_auth
                  ? 'amber'
                  : server.connected
                    ? 'green'
                    : 'gray';
              const meta = !server.config_enabled
                ? '配置已禁用'
                : !server.enabled
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
            {!loading && <div className="plugin-group">SKILLS · {visibleSkills.length}</div>}
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
                    // 端点 200 但 connected=false（例如 OAuth 仍缺授权）不是成功：
                    // 通知文案必须如实反映，不能一律说"已重新连接"。
                    const connected = await reconnectMcpServer(selectedServer.name);
                    setNotice(
                      connected
                        ? '已重新连接。'
                        : '重连未建立连接；如提示需要认证，请先在 CLI 完成 OAuth 登录。',
                    );
                  },
                  null,
                )
              }
              onDisconnect={() =>
                run(
                  `disconnect:${selectedServer.name}`,
                  async () => {
                    const disconnected = await disconnectMcpServer(selectedServer.name);
                    if (!disconnected) throw new Error('该 server 当前没有可断开的连接。');
                  },
                  // 断开是立即生效的连接动作，不是"重启后生效"的选择态。
                  '已断开连接；可随时点「重新连接」恢复。',
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
                run(`tool:${selectedServer.name}:${tool}`, async () => {
                  await setMcpToolEnabled(selectedServer.name, tool, enabled);
                  await refreshTools(selectedServer.name);
                })
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
  onDisconnect,
  onToggleEnabled,
  onToggleRequireConfirm,
  onToggleTool,
}: {
  server: McpServerEntry;
  tools: McpServerToolEntry[] | null;
  busy: string | null;
  onReconnect: () => void;
  onDisconnect: () => void;
  onToggleEnabled: (enabled: boolean) => void;
  onToggleRequireConfirm: (value: boolean) => void;
  onToggleTool: (tool: string, enabled: boolean) => void;
}) {
  // config 级 `enabled:false` 是配置源的事实，运行时选择态盖不过它：
  // 面板开关对它无效，状态必须说清"需改配置"，不能假装点一下就能启用。
  const configOff = !server.config_enabled;
  const status = configOff
    ? { label: '配置已禁用', tone: 'gray' }
    : !server.enabled
    ? { label: '已禁用', tone: 'gray' }
    : server.needs_auth
      ? { label: '需要认证', tone: 'amber' }
      : server.connected
        ? { label: '已连接', tone: 'green' }
        : { label: '未连接', tone: 'amber' };
  const enabledBusy = busy === `server:${server.name}`;
  const confirmBusy = busy === `confirm:${server.name}`;
  const reconnectBusy = busy === `reconnect:${server.name}`;
  const disconnectBusy = busy === `disconnect:${server.name}`;

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
          {/* 断开只在真的连着时可点：对未连接的 server 发断开是空操作。 */}
          <button
            type="button"
            className="plugin-btn"
            disabled={disconnectBusy || !server.connected}
            onClick={onDisconnect}
          >
            {disconnectBusy ? '断开中…' : '断开'}
          </button>
          <button
            type="button"
            className="plugin-btn"
            disabled={enabledBusy || configOff}
            title={configOff ? '该 server 在配置里被禁用；需修改配置后重启' : undefined}
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
            className="plugin-switch ui-switch"
            checked={server.enabled && !configOff}
            disabled={enabledBusy || configOff}
            onChange={(e) => onToggleEnabled(e.target.checked)}
          />
          启用此 server
        </label>
        {configOff && (
          <div className="plugin-meta">
            配置里标记为禁用（enabled: false）；修改配置并重启后才会接线。
          </div>
        )}
        <label className="switch-row">
          <input
            type="checkbox"
            className="plugin-switch ui-switch"
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
            className="plugin-switch ui-switch"
            checked={skill.enabled}
            disabled={busy}
            onChange={(e) => onToggleEnabled(e.target.checked)}
          />
          启用此 skill
        </label>
        {/* 裁决 D1：模型面（目录注入 / `load_skill`）下次装配才生效。菜单与面板是
            现读的只读投影，故刚禁用时会先从这里消失；但手敲 `/<name>` 的执行门仍按
            装配期目录，本轮内还会执行。说清这条时间差，不假装已经拦住。 */}
        <div className="plugin-meta">
          启停保存后菜单立即反映；模型目录与「/skill 名」的执行在下次装配后生效。
        </div>
      </div>
    </>
  );
}
