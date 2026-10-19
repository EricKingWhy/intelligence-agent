/** One-shot unattended runs: a separate page backed by the serve scheduler. */

import { useCallback, useEffect, useId, useState, type FormEvent } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import {
  ArrowLeft,
  Archive,
  CheckCircle2,
  Clock3,
  LoaderCircle,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Timer,
  TriangleAlert,
  X,
} from 'lucide-react';
import {
  actOnWatchtowerRule,
  createWatchtowerRule,
  getWatchtower,
  rescheduleWatchtowerRule,
  type CreateWatchtowerRulePayload,
  type ModelCatalogEntry,
  type SandboxBackendEntry,
  type WatchtowerRuleEntry,
  type WatchtowerRuleState,
  type WatchtowerSnapshot,
} from '../lib/api';
import '../styles/watchtower.css';

const CONSENT_VERSION = 'watchtower-consent-v1';
const REFRESH_INTERVAL_MS = 15_000;
const TOKEN_TIERS = [32_000, 64_000, 128_000];
const DURATION_TIERS = [
  { seconds: 1_800, label: '30 分钟' },
  { seconds: 7_200, label: '2 小时' },
  { seconds: 14_400, label: '4 小时' },
];

interface Props {
  onClose: () => void;
  onOpenSession: (sessionId: string) => void;
  models: ModelCatalogEntry[];
  sandboxBackends: SandboxBackendEntry[];
  defaultModel: string | null;
  defaultCwd: string | null;
  defaultSandboxBackend: string | null;
}

function browserTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
}

function toLocalDateTimeInput(value: Date): string {
  const local = new Date(value.getTime() - value.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function nextHourInput(): string {
  const nextHour = new Date();
  nextHour.setHours(nextHour.getHours() + 1, 0, 0, 0);
  return toLocalDateTimeInput(nextHour);
}

function countdown(target: string, now: number): string {
  const remaining = Math.max(0, Math.floor((Date.parse(target) - now) / 1000));
  const hours = Math.floor(remaining / 3600);
  const minutes = Math.floor((remaining % 3600) / 60);
  const seconds = remaining % 60;
  return [hours, minutes, seconds].map((value) => String(value).padStart(2, '0')).join(':');
}

function elapsed(target: string, now: number): string {
  const minutes = Math.max(1, Math.floor((now - Date.parse(target)) / 60_000));
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return hours > 0 ? `${hours} 小时 ${remainder} 分` : `${remainder} 分`;
}

function plannedTime(rule: WatchtowerRuleEntry): string {
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: rule.timezone,
  }).format(new Date(rule.trigger_at));
}

function stateLabel(state: WatchtowerRuleState): string {
  const labels: Record<WatchtowerRuleState, string> = {
    active: '待触发',
    armed: '待触发',
    paused: '已暂停',
    dispatching: '正在启动',
    fired: '已触发',
    expired: '已过期',
    launch_failed: '启动失败',
    archived: '已归档',
  };
  return labels[state];
}

function budgetSummary(rule: WatchtowerRuleEntry): string {
  const parts: string[] = [];
  if (rule.budget_turns !== null) parts.push(`${rule.budget_turns} 轮`);
  if (rule.budget_tokens !== null) parts.push(`${rule.budget_tokens.toLocaleString()} 字`);
  if (rule.budget_duration_seconds !== null) {
    const duration = DURATION_TIERS.find((item) => item.seconds === rule.budget_duration_seconds);
    parts.push(duration?.label ?? `${Math.round(rule.budget_duration_seconds / 60)} 分钟`);
  }
  return parts.length > 0 ? parts.join(' · ') : '默认预算';
}

function promptSummary(prompt: string): string {
  const compact = prompt.replace(/\s+/g, ' ').trim();
  return compact.length > 140 ? `${compact.slice(0, 140)}…` : compact;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请检查连接后重试。';
}

export function WatchtowerPanel({
  onClose,
  onOpenSession,
  models,
  sandboxBackends,
  defaultModel,
  defaultCwd,
  defaultSandboxBackend,
}: Props) {
  const [snapshot, setSnapshot] = useState<WatchtowerSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyRuleId, setBusyRuleId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [rescheduleRule, setRescheduleRule] = useState<WatchtowerRuleEntry | null>(null);
  const [now, setNow] = useState(Date.now());

  const refresh = useCallback(async () => {
    try {
      setSnapshot(await getWatchtower());
      setLoadError(null);
    } catch (error) {
      setLoadError(errorMessage(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const poll = window.setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    const clock = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => {
      window.clearInterval(poll);
      window.clearInterval(clock);
    };
  }, [refresh]);

  const act = async (ruleId: string, action: 'pause' | 'resume' | 'archive' | 'retry') => {
    setBusyRuleId(ruleId);
    setActionError(null);
    try {
      await actOnWatchtowerRule(ruleId, action);
      await refresh();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusyRuleId(null);
    }
  };

  const saveReschedule = async (rule: WatchtowerRuleEntry, localTime: string) => {
    setBusyRuleId(rule.rule_id);
    setActionError(null);
    try {
      await rescheduleWatchtowerRule(rule.rule_id, {
        local_time: localTime,
        timezone: browserTimezone(),
      });
      setRescheduleRule(null);
      await refresh();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusyRuleId(null);
    }
  };

  const rules = snapshot?.rules ?? [];
  const upcoming = rules.filter((rule) =>
    ['active', 'armed', 'paused', 'dispatching'].includes(rule.state),
  );
  const attention = rules.filter((rule) =>
    rule.state === 'expired' || rule.state === 'launch_failed',
  );
  const history = rules.filter((rule) => rule.state === 'fired' || rule.state === 'archived');
  const scheduler = snapshot?.scheduler;

  return (
    <section className="watchtower-page" aria-label="定时任务面板">
      <header className="watchtower-page-header">
        <div className="watchtower-heading">
          <button className="watchtower-back" onClick={onClose} aria-label="返回工作区">
            <ArrowLeft size={16} aria-hidden="true" />
            <span>返回工作区</span>
          </button>
          <div>
            <p className="watchtower-eyebrow">定时任务 · 单次执行</p>
            <h1>定时任务</h1>
            <p className="watchtower-subtitle">
              单次延时执行 · 到点新开会话 · 跑完待验证，由你亲自验收
            </p>
          </div>
        </div>
        <div className="watchtower-header-actions">
          <span
            className={`watchtower-scheduler-pill ${
              scheduler?.running && !scheduler.error
                ? 'is-running'
                : scheduler?.error
                  ? 'has-error'
                  : 'is-stopped'
            }`}
            role="status"
          >
            <span className="watchtower-status-dot" aria-hidden="true" />
            {scheduler?.running && !scheduler.error
              ? '调度器运行中 · 单实例'
              : scheduler?.error
                ? `调度异常 · ${scheduler.error}`
                : loading
                  ? '读取调度器状态…'
                  : '调度器未运行'}
          </span>
          <button className="watchtower-primary" onClick={() => setCreateOpen(true)}>
            <Plus size={16} aria-hidden="true" />
            新建定时任务
          </button>
        </div>
      </header>

      {scheduler?.last_tick_at && (
        <p className="watchtower-heartbeat">
          最近检查：{new Date(scheduler.last_tick_at).toLocaleString()}
        </p>
      )}
      {loadError && (
        <div className="watchtower-message is-error" role="alert">
          <TriangleAlert size={15} aria-hidden="true" />
          <span>{loadError}</span>
          <button className="watchtower-inline-button" onClick={() => void refresh()}>
            重试
          </button>
        </div>
      )}
      {actionError && <div className="watchtower-message is-error" role="alert">{actionError}</div>}

      {(snapshot?.pending_verification.length ?? 0) > 0 && (
        <section className="watchtower-verification" aria-label="待验证会话">
          <div className="watchtower-verification-icon"><CheckCircle2 size={17} aria-hidden="true" /></div>
          <div className="watchtower-verification-copy">
            <strong>{snapshot!.pending_verification.length} 个定时任务会话等待你验收</strong>
            <span>定时任务只负责执行，结果是否合格由你决定。</span>
          </div>
          <div className="watchtower-verification-links">
            {snapshot!.pending_verification.map((item) => (
              <button
                key={item.session_id}
                className="watchtower-inline-button"
                onClick={() => onOpenSession(item.session_id)}
              >
                查看 {item.session_id.slice(0, 8)}
              </button>
            ))}
          </div>
        </section>
      )}

      {loading && snapshot === null ? (
        <div className="watchtower-empty"><LoaderCircle size={18} className="watchtower-spin" />正在读取定时任务规则…</div>
      ) : (
        <div className="watchtower-groups">
          <RuleGroup
            title="待触发"
            description="到点后由 serve 自动启动新会话。暂停期间到期的规则会转为过期。"
            rules={upcoming}
            empty="暂无待触发规则。"
            now={now}
            busyRuleId={busyRuleId}
            onAction={act}
            onReschedule={setRescheduleRule}
            onOpenSession={onOpenSession}
          />
          <RuleGroup
            title="需要处理"
            description="过期规则可改期；启动失败可手动重跑，不会自动顺延。"
            rules={attention}
            empty="没有需要处理的规则。"
            now={now}
            busyRuleId={busyRuleId}
            onAction={act}
            onReschedule={setRescheduleRule}
            onOpenSession={onOpenSession}
          />
          <RuleGroup
            title="已归档"
            description="已触发的会话保留在这里，方便回看并进入人工验收。"
            rules={history}
            empty="触发或归档后，规则会出现在这里。"
            now={now}
            busyRuleId={busyRuleId}
            onAction={act}
            onReschedule={setRescheduleRule}
            onOpenSession={onOpenSession}
          />
        </div>
      )}

      <CreateWatchtowerDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={() => {
          setCreateOpen(false);
          void refresh();
        }}
        models={models}
        sandboxBackends={sandboxBackends}
        defaultModel={defaultModel}
        defaultCwd={defaultCwd}
        defaultSandboxBackend={defaultSandboxBackend}
      />
      <RescheduleDialog
        rule={rescheduleRule}
        busy={rescheduleRule !== null && busyRuleId === rescheduleRule.rule_id}
        onClose={() => setRescheduleRule(null)}
        onSave={(localTime) => {
          if (rescheduleRule) void saveReschedule(rescheduleRule, localTime);
        }}
      />
    </section>
  );
}

interface RuleGroupProps {
  title: string;
  description: string;
  rules: WatchtowerRuleEntry[];
  empty: string;
  now: number;
  busyRuleId: string | null;
  onAction: (ruleId: string, action: 'pause' | 'resume' | 'archive' | 'retry') => void;
  onReschedule: (rule: WatchtowerRuleEntry) => void;
  onOpenSession: (sessionId: string) => void;
}

function RuleGroup({
  title,
  description,
  rules,
  empty,
  now,
  busyRuleId,
  onAction,
  onReschedule,
  onOpenSession,
}: RuleGroupProps) {
  return (
    <section className="watchtower-group" aria-label={title}>
      <div className="watchtower-group-heading">
        <h2>{title}<span className="watchtower-count">{rules.length}</span></h2>
        <p>{description}</p>
      </div>
      {rules.length === 0 ? (
        <div className="watchtower-empty">{empty}</div>
      ) : (
        <div className="watchtower-rule-list">
          {rules.map((rule) => {
            const busy = busyRuleId === rule.rule_id;
            const execution = rule.executions[rule.executions.length - 1];
            const hasSession = execution?.state === 'fired' && execution.session_id !== null;
            return (
              <article className={`watchtower-rule is-${rule.state}`} key={rule.rule_id}>
                <div className="watchtower-rule-main">
                  <div className="watchtower-rule-titleline">
                    <span className={`watchtower-rule-state is-${rule.state}`}>{stateLabel(rule.state)}</span>
                    <p className="watchtower-rule-prompt" title={rule.prompt}>{promptSummary(rule.prompt)}</p>
                  </div>
                  {rule.state === 'expired' ? (
                    <p className="watchtower-rule-time is-warning">
                      已过期 {elapsed(rule.trigger_at, now)} · 宽限窗（5 分钟）已过，未补跑
                    </p>
                  ) : rule.state === 'launch_failed' ? (
                    <p className="watchtower-rule-time is-danger">
                      未顺延 · 未产生会话 · {rule.last_error ?? '可手动立即重跑'}
                    </p>
                  ) : rule.state === 'active' || rule.state === 'armed' ? (
                    <p className="watchtower-rule-time">
                      <Timer size={14} aria-hidden="true" />
                      <strong>{countdown(rule.trigger_at, now)}</strong>
                      {rule.trigger_kind === 'absolute' && (
                        <span>{plannedTime(rule)} · {rule.timezone}</span>
                      )}
                      {rule.trigger_kind === 'relative' && <span>后触发</span>}
                    </p>
                  ) : rule.state === 'paused' ? (
                    <p className="watchtower-rule-time is-muted">
                      <Pause size={13} aria-hidden="true" /> 暂停中 · 原计划 {plannedTime(rule)} · 到期后标为过期
                    </p>
                  ) : rule.state === 'dispatching' ? (
                    <p className="watchtower-rule-time">
                      <LoaderCircle size={13} className="watchtower-spin" aria-hidden="true" />
                      正在创建会话并启动
                    </p>
                  ) : (
                    <p className="watchtower-rule-time is-muted">
                      <Clock3 size={13} aria-hidden="true" /> 触发于 {plannedTime(rule)}
                    </p>
                  )}
                  <div className="watchtower-rule-meta">
                    <span>{budgetSummary(rule)}</span>
                    <span>自动档 · 等待超时不会授权</span>
                    <span>{rule.sandbox_backend ? `沙箱 · ${rule.sandbox_backend}` : '沙箱 · 部署默认'}</span>
                    <span>{rule.model ?? '默认模型链'}</span>
                  </div>
                  {rule.last_notice && <p className="watchtower-notice" role="status">{rule.last_notice}</p>}
                  {hasSession && execution.session_id && (
                    <button
                      className="watchtower-session-link"
                      onClick={() => onOpenSession(execution.session_id!)}
                    >
                      会话 · {execution.product_state === 'pending_verification' ? '待验证' : '查看详情'}
                      <span className="mono">{execution.session_id.slice(0, 8)}</span>
                    </button>
                  )}
                </div>
                <div className="watchtower-rule-actions">
                  {(rule.state === 'active' || rule.state === 'armed') && (
                    <button className="watchtower-action" onClick={() => onAction(rule.rule_id, 'pause')} disabled={busy}>
                      {busy ? <LoaderCircle size={14} className="watchtower-spin" /> : <Pause size={14} />}
                      暂停
                    </button>
                  )}
                  {rule.state === 'paused' && (
                    <button className="watchtower-action" onClick={() => onAction(rule.rule_id, 'resume')} disabled={busy}>
                      {busy ? <LoaderCircle size={14} className="watchtower-spin" /> : <Play size={14} />}
                      恢复
                    </button>
                  )}
                  {rule.state === 'expired' && (
                    <button className="watchtower-action is-primary" onClick={() => onReschedule(rule)} disabled={busy}>
                      改期重跑
                    </button>
                  )}
                  {rule.state === 'launch_failed' && (
                    <button className="watchtower-action is-primary" onClick={() => onAction(rule.rule_id, 'retry')} disabled={busy}>
                      {busy ? <LoaderCircle size={14} className="watchtower-spin" /> : <RefreshCw size={14} />}
                      立即重跑
                    </button>
                  )}
                  {rule.state !== 'dispatching' && rule.state !== 'archived' && (
                    <button
                      className="watchtower-action is-quiet"
                      onClick={() => onAction(rule.rule_id, 'archive')}
                      disabled={busy}
                    >
                      <Archive size={14} />{rule.state === 'fired' ? '归档' : '取消'}
                    </button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

interface CreateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
  models: ModelCatalogEntry[];
  sandboxBackends: SandboxBackendEntry[];
  defaultModel: string | null;
  defaultCwd: string | null;
  defaultSandboxBackend: string | null;
}

function CreateWatchtowerDialog(props: CreateDialogProps) {
  return (
    <Dialog.Root open={props.open} onOpenChange={props.onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="watchtower-dialog-overlay" />
        {props.open && <CreateWatchtowerForm {...props} />}
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function CreateWatchtowerForm({
  onOpenChange,
  onCreated,
  models,
  sandboxBackends,
  defaultModel,
  defaultCwd,
  defaultSandboxBackend,
}: CreateDialogProps) {
  const promptId = useId();
  const delayId = useId();
  const absoluteId = useId();
  const turnsId = useId();
  const tokensId = useId();
  const durationId = useId();
  const modelId = useId();
  const sandboxId = useId();
  const cwdId = useId();
  const timezone = browserTimezone();
  const [prompt, setPrompt] = useState('');
  const [triggerKind, setTriggerKind] = useState<'relative' | 'absolute'>('relative');
  const [delayValue, setDelayValue] = useState('2');
  const [delayUnit, setDelayUnit] = useState<'minutes' | 'hours' | 'days'>('hours');
  const [localTime, setLocalTime] = useState(nextHourInput);
  const [turns, setTurns] = useState('');
  const [tokens, setTokens] = useState('');
  const [duration, setDuration] = useState('');
  const [model, setModel] = useState(defaultModel ?? '');
  const [sandbox, setSandbox] = useState(defaultSandboxBackend ?? '');
  const [cwd, setCwd] = useState(defaultCwd ?? '');
  const [consent, setConsent] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting || !consent || !prompt.trim()) return;
    const delay = Number(delayValue) * (
      delayUnit === 'days' ? 86_400 : delayUnit === 'hours' ? 3_600 : 60
    );
    const turnsValue = turns.trim() ? Number(turns) : null;
    const tokensValue = tokens ? Number(tokens) : null;
    if (triggerKind === 'relative' && (!Number.isSafeInteger(delay) || delay < 60)) {
      setError('相对触发时间至少为 1 分钟。');
      return;
    }
    if (turnsValue !== null && (!Number.isSafeInteger(turnsValue) || turnsValue < 1)) {
      setError('轮数上限必须是正整数。');
      return;
    }
    setSubmitting(true);
    setError(null);
    const payload: CreateWatchtowerRulePayload = {
      prompt: prompt.trim(),
      trigger_kind: triggerKind,
      timezone,
      ...(triggerKind === 'relative' ? { delay_seconds: delay } : { local_time: localTime }),
      model: model || null,
      sandbox_backend: sandbox || null,
      cwd: cwd.trim() || null,
      budget_turns: turnsValue,
      budget_tokens: tokensValue,
      budget_duration_seconds: duration ? Number(duration) : null,
      consent_acknowledged: consent,
      consent_version: CONSENT_VERSION,
    };
    try {
      await createWatchtowerRule(payload);
      onCreated();
    } catch (cause) {
      setError(errorMessage(cause));
      setSubmitting(false);
    }
  };

  const availableBackends = sandboxBackends.filter((entry) => entry.available);
  const selectedBackendMissing = sandbox !== ''
    && !sandboxBackends.some((entry) => entry.backend === sandbox);
  const selectedModelMissing = model !== '' && !models.some((entry) => entry.name === model);

  return (
    <Dialog.Content className="watchtower-dialog" aria-labelledby="watchtower-create-title">
      <div className="watchtower-dialog-heading">
        <div>
          <Dialog.Title id="watchtower-create-title">新建定时任务</Dialog.Title>
          <Dialog.Description>安排一次未来自动执行的任务。</Dialog.Description>
        </div>
        <Dialog.Close asChild>
          <button className="watchtower-dialog-close" aria-label="关闭新建定时任务">
            <X size={17} />
          </button>
        </Dialog.Close>
      </div>
      <form onSubmit={(event) => void submit(event)}>
        <label className="watchtower-field" htmlFor={promptId}>
          <span>任务描述 <b>必填</b></span>
          <textarea
            id={promptId}
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder="描述希望 agent 到点后完成的工作…"
            rows={4}
            required
            autoFocus
          />
        </label>

        <fieldset className="watchtower-fieldset">
          <legend>触发时间</legend>
          <div className="watchtower-segment" role="group" aria-label="触发时间类型">
            <button type="button" aria-pressed={triggerKind === 'relative'} onClick={() => setTriggerKind('relative')}>
              相对时间
            </button>
            <button type="button" aria-pressed={triggerKind === 'absolute'} onClick={() => setTriggerKind('absolute')}>
              指定时刻
            </button>
          </div>
          {triggerKind === 'relative' ? (
            <div className="watchtower-trigger-inputs">
              <label htmlFor={delayId}>多久后触发</label>
              <input
                id={delayId}
                type="number"
                min={1}
                value={delayValue}
                onChange={(event) => setDelayValue(event.target.value)}
              />
              <select
                aria-label="相对时间单位"
                value={delayUnit}
                onChange={(event) => setDelayUnit(event.target.value as typeof delayUnit)}
              >
                <option value="minutes">分钟后</option>
                <option value="hours">小时后</option>
                <option value="days">天后</option>
              </select>
            </div>
          ) : (
            <label className="watchtower-field" htmlFor={absoluteId}>
              <span>本地时间 <b>{timezone}</b></span>
              <input
                id={absoluteId}
                type="datetime-local"
                value={localTime}
                onChange={(event) => setLocalTime(event.target.value)}
                min={toLocalDateTimeInput(new Date())}
                required
              />
              <small>创建后锁定该时区。夏令时跳时的本地时刻无法创建；重复时刻取较早的一次。</small>
            </label>
          )}
        </fieldset>

        <fieldset className="watchtower-fieldset">
          <legend>运行设置</legend>
          <div className="watchtower-form-grid">
            <label className="watchtower-field" htmlFor={turnsId}>
              <span>轮数上限</span>
              <input id={turnsId} type="number" min={1} value={turns} onChange={(event) => setTurns(event.target.value)} placeholder="默认" />
            </label>
            <label className="watchtower-field" htmlFor={tokensId}>
              <span>字数上限</span>
              <select id={tokensId} value={tokens} onChange={(event) => setTokens(event.target.value)}>
                <option value="">默认</option>
                {TOKEN_TIERS.map((tier) => <option key={tier} value={tier}>{tier.toLocaleString()} 字</option>)}
              </select>
            </label>
            <label className="watchtower-field" htmlFor={durationId}>
              <span>运行时长</span>
              <select id={durationId} value={duration} onChange={(event) => setDuration(event.target.value)}>
                <option value="">不设上限</option>
                {DURATION_TIERS.map((tier) => <option key={tier.seconds} value={tier.seconds}>{tier.label}</option>)}
              </select>
            </label>
            {(models.length > 0 || selectedModelMissing) && (
              <label className="watchtower-field" htmlFor={modelId}>
                <span>模型</span>
                <select id={modelId} value={model} onChange={(event) => setModel(event.target.value)}>
                  <option value="">默认模型链</option>
                  {selectedModelMissing && <option value={model}>{model} · 当前会话</option>}
                  {models.map((entry) => <option key={entry.name} value={entry.name}>{entry.name}</option>)}
                </select>
              </label>
            )}
            {(availableBackends.length > 0 || selectedBackendMissing) && (
              <label className="watchtower-field" htmlFor={sandboxId}>
                <span>沙箱</span>
                <select id={sandboxId} value={sandbox} onChange={(event) => setSandbox(event.target.value)}>
                  <option value="">部署默认</option>
                  {selectedBackendMissing && <option value={sandbox}>{sandbox} · 当前会话</option>}
                  {sandboxBackends.map((entry) => (
                    <option key={entry.backend} value={entry.backend} disabled={!entry.available && sandbox !== entry.backend}>
                      {entry.backend === 'local' ? '本机' : entry.backend === 'docker' ? 'Docker' : entry.backend}
                      {!entry.available ? ' · 当前不可用' : ''}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="watchtower-field watchtower-field-wide" htmlFor={cwdId}>
              <span>工作目录 <b>触发时使用此快照</b></span>
              <input id={cwdId} value={cwd} onChange={(event) => setCwd(event.target.value)} placeholder="部署默认工作目录" />
            </label>
          </div>
          <p className="watchtower-budget-note">运行时长从实际触发启动时起算；触发等待时间不会消耗预算。</p>
        </fieldset>

        <section className="watchtower-consent-box" aria-labelledby="watchtower-consent-title">
          <h3 id="watchtower-consent-title">定时任务使用自动权限</h3>
          <ul>
            <li>定时任务可以等待你的回答，但任务必须仍有足够的轮数 / 字数预算，且没有设定截止时间。只要设了时长预算，定时任务就不能等待你的回答——时长预算从触发时开始倒计时。</li>
            <li>允许等待时，通用提问超时后不写入答案，任务收到“用户可能不在”的提示后继续。</li>
            <li>要求冲突超时后不会替你选择或授权，任务收到未解决提示并停止受影响的工作，等你处理。</li>
            <li>删除文件、发外部请求、git push 等危险操作会自动放行；此期间没有人逐次复核。</li>
            <li>仍使用你选定的沙箱。运行结果只进入「待验证」，不会自动验收或标记为通过。</li>
          </ul>
          <label className="watchtower-consent-check">
            <input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />
            <span>我已知晓并同意以上内容，定时任务触发后的操作后果由我承担。</span>
          </label>
        </section>

        {error && <p className="watchtower-form-error" role="alert">{error}</p>}
        <div className="watchtower-dialog-actions">
          <button type="button" className="watchtower-action" onClick={() => onOpenChange(false)} disabled={submitting}>取消</button>
          <button type="submit" className="watchtower-primary" disabled={!consent || !prompt.trim() || submitting}>
            {submitting ? <LoaderCircle size={15} className="watchtower-spin" /> : <Plus size={15} />}
            创建定时任务
          </button>
        </div>
      </form>
    </Dialog.Content>
  );
}

interface RescheduleDialogProps {
  rule: WatchtowerRuleEntry | null;
  busy: boolean;
  onClose: () => void;
  onSave: (localTime: string) => void;
}

function RescheduleDialog({ rule, busy, onClose, onSave }: RescheduleDialogProps) {
  const [localTime, setLocalTime] = useState(nextHourInput);
  useEffect(() => {
    if (rule) setLocalTime(nextHourInput());
  }, [rule]);
  return (
    <Dialog.Root open={rule !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="watchtower-dialog-overlay" />
        {rule && (
          <Dialog.Content className="watchtower-dialog watchtower-reschedule-dialog">
            <div className="watchtower-dialog-heading">
              <div>
                <Dialog.Title>改期重跑</Dialog.Title>
                <Dialog.Description>{promptSummary(rule.prompt)}</Dialog.Description>
              </div>
              <Dialog.Close asChild><button className="watchtower-dialog-close" aria-label="关闭改期"><X size={17} /></button></Dialog.Close>
            </div>
            <label className="watchtower-field">
              <span>新的触发时刻 <b>{browserTimezone()}</b></span>
              <input
                type="datetime-local"
                value={localTime}
                onChange={(event) => setLocalTime(event.target.value)}
                min={toLocalDateTimeInput(new Date())}
              />
            </label>
            <div className="watchtower-dialog-actions">
              <button className="watchtower-action" onClick={onClose} disabled={busy}>取消</button>
              <button
                className="watchtower-primary"
                onClick={() => onSave(localTime)}
                disabled={busy || !localTime}
              >
                {busy ? <LoaderCircle size={15} className="watchtower-spin" /> : <RefreshCw size={15} />}
                保存新时刻
              </button>
            </div>
          </Dialog.Content>
        )}
      </Dialog.Portal>
    </Dialog.Root>
  );
}
