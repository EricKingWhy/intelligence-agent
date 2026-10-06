/** 新建任务统一入口（#367 [W-23] 选项 A，用户 2026-10-06 批准）。
 *
 *  六家成熟产品共识（研究报告 `~/workspace/issue-367-study/mature-products.md`）：
 *  prompt 优先、无多步向导、无结构化验收项、无任务级排队、三档自主度。
 *  本弹窗即按此共识的信息架构：
 *  - prompt 是**唯一必填项**（顶置大输入框）
 *  - 验收项降级为可选文本框，内容拼进首条 prompt（零服务端契约变更，
 *    对标 Codex `/goal` 自然语言三要素）
 *  - 三档自主度抄 Copilot Interactive / Plan / Autopilot
 *    （≈ Codex 三档 ≈ Claude Code modes）
 *  - 目录冲突默认 worktree（后端 `on_conflict` 默认值，前端不另设）
 *
 *  取代 `StartTaskInProjectDialog` 的"空会话"流程（#204 的"弹窗不该有任务内容"
 *  裁定被选项 A 取代：prompt 进弹窗，创建即带任务启动）。
 *
 *  表单状态放只在打开时挂载的内层组件（与 ProjectDialogs 同一条结构纪律：
 *  挂载即初始化，关闭即丢弃）。
 */

import { useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Cpu, HardDrive, TriangleAlert, X } from 'lucide-react';
import type { ModelCatalogEntry, SandboxBackendEntry } from '../lib/api';
import type { Project } from '../types';
import type { StartSessionPayload } from '../lib/api';
import { OptionPicker } from './OptionPicker';

/** 三档自主度（抄 Copilot Interactive / Plan / Autopilot 的命名与语义）。 */
const AUTONOMY_OPTIONS = [
  {
    id: 'ask',
    label: '问',
    description: '每个工具调用都先问你（读文件不问）',
  },
  {
    id: 'plan',
    label: '计划',
    description: '先出计划，你批准后再执行',
  },
  {
    id: 'auto',
    label: '自动',
    description: '全自动执行，不逐次询问',
  },
] as const;

type AutonomyId = (typeof AUTONOMY_OPTIONS)[number]['id'];

interface Props {
  /** 待开任务的项目；null = 关闭。 */
  project: Project | null;
  /** 模型目录（GET /api/models）。空数组 = 端点缺席 → 模型选择器降级隐藏。 */
  models: ModelCatalogEntry[];
  /** sandbox 后端探针（GET /api/sandbox-backends）。空数组 → 运行位置隐藏。 */
  sandboxBackends: SandboxBackendEntry[];
  onOpenChange: (open: boolean) => void;
  /** 创建并启动任务。resolve `null` = 已创建（调用方关闭浮层）；
   *  否则 resolve 一句给用户看的原因（浮层就地显示）。 */
  onCreateTask: (payload: StartSessionPayload) => Promise<string | null>;
}

export function TaskCreationDialog({
  project,
  models,
  sandboxBackends,
  onOpenChange,
  onCreateTask,
}: Props) {
  return (
    <Dialog.Root
      open={project !== null}
      onOpenChange={(open) => {
        if (!open) onOpenChange(false);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        {project && (
          <TaskCreationForm
            project={project}
            models={models}
            sandboxBackends={sandboxBackends}
            onOpenChange={onOpenChange}
            onCreateTask={onCreateTask}
          />
        )}
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function TaskCreationForm({
  project,
  models,
  sandboxBackends,
  onOpenChange,
  onCreateTask,
}: Props & { project: Project }) {
  // prompt 是唯一必填项
  const [prompt, setPrompt] = useState('');
  // 验收项：可选文本框，提交时拼进首条 prompt（零服务端契约变更）
  const [acceptance, setAcceptance] = useState('');
  const [autonomy, setAutonomy] = useState<AutonomyId>('ask');
  // 模型：'auto' = 不发 model 键（后端默认链）；其余为目录 name
  const [model, setModel] = useState<string>('auto');
  // 运行位置：'auto' = 不发 sandbox_backend 键（部署默认）；其余为后端名
  const [sandbox, setSandbox] = useState<string>('auto');
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const promptValid = prompt.trim().length > 0;

  const submit = async () => {
    if (pending || !promptValid) return;
    setPending(true);
    setError(null);
    // 验收项拼进首条 prompt（Codex `/goal` 自然语言三要素同型）
    const task = acceptance.trim()
      ? `完成目标：${acceptance.trim()}\n\n${prompt.trim()}`
      : prompt.trim();
    const payload: StartSessionPayload = {
      task,
      cwd: project.path,
      autonomy,
      ...(model !== 'auto' ? { model } : {}),
      ...(sandbox !== 'auto' ? { sandbox_backend: sandbox } : {}),
      // on_conflict 不发 = 后端默认 worktree（选项 A：目录冲突默认自动 worktree）
    };
    const failure = await onCreateTask(payload);
    if (failure) {
      setError(failure);
      setPending(false);
      return;
    }
    onOpenChange(false);
  };

  const modelOptions = [
    { value: 'auto', title: 'Auto', description: '默认模型链' },
    ...models.map((m) => ({
      value: m.name,
      title: m.display_name || m.name,
      description: m.description || undefined,
    })),
  ];

  const sandboxOptions = [
    { value: 'auto', title: '默认', description: '部署默认（本机）' },
    ...sandboxBackends
      .filter((b) => b.available)
      .map((b) => ({
        value: b.backend,
        title: b.backend === 'local' ? '本机' : b.backend === 'docker' ? 'Docker' : b.backend,
        description: undefined,
      })),
  ];
  // 不可用的后端不进选项（不画饼）；但若之前选过已变不可用的，后端会 409 结构化报错
  const unavailableBackends = sandboxBackends.filter((b) => !b.available);

  return (
    <Dialog.Content className="project-dialog" aria-label="新建任务">
      <div className="project-dialog-head">
        <Dialog.Title className="project-dialog-title">
          新建任务
        </Dialog.Title>
        <Dialog.Close asChild>
          <button className="icon-btn project-dialog-close" aria-label="关闭">
            <X size={14} />
          </button>
        </Dialog.Close>
      </div>

      <Dialog.Description className="project-dialog-desc">
        在项目「{project.title}」里开一个新任务，创建即启动。
      </Dialog.Description>

      {/* 路径明示行：承担"你正在把哪个目录交给 agent"的确认责任 */}
      <div className="project-path-callout" role="note">
        <TriangleAlert size={13} aria-hidden="true" />
        <span>
          Agent 将直接读写该目录：
          <span className="project-path-callout-path mono">{project.path}</span>
        </span>
      </div>

      {/* prompt：唯一必填项 */}
      <div className="project-field">
        <label className="project-field-label" htmlFor="task-prompt">
          任务描述 <span aria-hidden="true">*</span>
        </label>
        <textarea
          id="task-prompt"
          className="project-textarea"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="让 agent 做什么？（例如：给登录接口加上限流，顺手补个测试）"
          rows={4}
          disabled={pending}
          autoFocus
        />
      </div>

      {/* 验收项：可选文本框，拼进首条 prompt */}
      <div className="project-field">
        <label className="project-field-label" htmlFor="task-acceptance">
          完成目标 <span className="project-field-optional">（可选）</span>
        </label>
        <textarea
          id="task-acceptance"
          className="project-textarea"
          value={acceptance}
          onChange={(e) => setAcceptance(e.target.value)}
          placeholder="怎么算做完？（例如：pytest 全绿，覆盖率不下降）"
          rows={2}
          disabled={pending}
        />
        <span className="project-field-hint">
          会拼在任务描述前面一起发给 agent，不单独存。
        </span>
      </div>

      {/* 三档自主度 */}
      <div className="project-field">
        <span className="project-field-label" id="autonomy-label">自主度</span>
        <div
          className="segmented"
          role="radiogroup"
          aria-labelledby="autonomy-label"
        >
          {AUTONOMY_OPTIONS.map((opt) => (
            <button
              key={opt.id}
              type="button"
              role="radio"
              aria-checked={autonomy === opt.id}
              className={`segmented-option${autonomy === opt.id ? ' selected' : ''}`}
              onClick={() => setAutonomy(opt.id)}
              disabled={pending}
              title={opt.description}
            >
              {opt.label}
            </button>
          ))}
        </div>
        <span className="project-field-hint">
          {AUTONOMY_OPTIONS.find((o) => o.id === autonomy)?.description}
        </span>
      </div>

      {/* 模型（含 Auto） */}
      {models.length > 0 && (
        <div className="project-field">
          <span className="project-field-label">模型</span>
          <OptionPicker
            ariaLabel="模型"
            title="用哪个模型跑这个任务？"
            icon={Cpu}
            options={modelOptions}
            value={model}
            onChange={(id) => setModel(id ?? 'auto')}
            placeholder="Auto（默认）"
            disabled={pending}
          />
        </div>
      )}

      {/* 运行位置 */}
      {sandboxBackends.length > 0 && (
        <div className="project-field">
          <span className="project-field-label">运行位置</span>
          <OptionPicker
            ariaLabel="运行位置"
            title="任务在哪里跑？"
            icon={HardDrive}
            options={sandboxOptions}
            value={sandbox}
            onChange={(id) => setSandbox(id ?? 'auto')}
            placeholder="默认"
            disabled={pending}
          />
          {unavailableBackends.length > 0 && (
            <span className="project-field-hint">
              不可用：{unavailableBackends.map((b) =>
                `${b.backend}（${b.reason ?? '未知原因'}）`).join('、')}
            </span>
          )}
          <span className="project-field-hint">
            目录被其他任务占用时，默认自动建 worktree 并行（可在创建请求里改排队）。
          </span>
        </div>
      )}

      {error && (
        <div className="project-error" role="alert">
          {error}
        </div>
      )}

      <div className="project-dialog-actions">
        <Dialog.Close asChild>
          <button className="project-btn">取消</button>
        </Dialog.Close>
        <button
          className="project-btn project-btn-primary"
          onClick={() => void submit()}
          disabled={pending || !promptValid}
        >
          {pending ? '正在创建…' : '创建任务'}
        </button>
      </div>
    </Dialog.Content>
  );
}
