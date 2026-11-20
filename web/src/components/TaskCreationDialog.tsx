/** 新建任务统一入口（#367 [W-23] 选项 A，用户 2026-10-06 批准；#1177 §B 化简）。
 *
 *  六家成熟产品共识（研究报告 `~/workspace/issue-367-study/mature-products.md`）：
 *  prompt 优先、无多步向导、无结构化验收项、无任务级排队、三档自主度。
 *  本弹窗即按此共识的信息架构：
 *  - prompt 是**唯一必填项**（顶置大输入框）
 *  - 验收项降级为可选**单行**输入框，作为结构化 `criteria` 随创建请求提交
 *    （#1359 阶段 2 起不再拼进 prompt；对标 omp `/guided-goal` 的结构化采集）
 *  - 三档自主度抄 Copilot Interactive / Plan / Autopilot
 *    （≈ Codex 三档 ≈ Claude Code modes）
 *  - 目录冲突默认 worktree（后端 `on_conflict` 默认值，前端不另设）
 *
 *  #1177 §B（用户 2026-10-15 审批的效果图）：**任务描述是唯一主视觉**，完成目标 /
 *  自主度 / 模型 / 运行位置收进「高级设置」折叠区，默认折叠（composer-first 的产品
 *  把设置后置到输入框旁，本仓保留模态但把设置收进折叠——Cursor / VS Code Copilot
 *  Chat / Claude Code / 腾讯 CodeBuddy 的清一色做法，见
 *  `docs/research/2026-10-16-issue-1177-new-task-dialog-plan.md` §2）。路径警告保留，
 *  但收窄为单行（安全相关，不删除）。「创建即启动」语义一字未动。
 *
 *  取代 `StartTaskInProjectDialog` 的"空会话"流程（#204 的"弹窗不该有任务内容"
 *  裁定被选项 A 取代：prompt 进弹窗，创建即带任务启动）。
 *
 *  表单状态放只在打开时挂载的内层组件（与 ProjectDialogs 同一条结构纪律：
 *  挂载即初始化，关闭即丢弃）。
 */

import { useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { ChevronRight, Cpu, HardDrive, TriangleAlert, X } from 'lucide-react';
import type { ModelCatalogEntry, SandboxBackendEntry } from '../lib/api';
import type { Project } from '../types';
import type { StartSessionFailure, StartSessionPayload, SubmitTaskResult } from '../lib/api';
import { OptionPicker } from './OptionPicker';
import { deferMenuInitialFocus, focusFirstIn } from './menuDialogTiming';

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

/** #1644（票面定义的唯一来源，#1486 对齐本票而不是反向）：local 后端的两条明示
 *  文案。前端没有平台探测能力，不猜 `navigator.platform`：通用句放在运行位置
 *  local 选项的描述里，Windows 追加句与通用句一并展示在 409 错误卡片上
 *  （用户自己核对平台）。
 *
 *  `LOCAL_BACKEND_NOTICE`：通用句，逐字出现在 local 选项与 409 卡片。
 *  `LOCAL_BACKEND_NTFS_NOTICE`：Windows 追加句（硬链接逃逸风险，见 #1048），
 *  只在 409 卡片出现。两处引用同一常量：grep 可查、单一来源。 */
export const LOCAL_BACKEND_NOTICE =
  'local 后端直接在本机文件系统运行，无操作系统级文件/网络隔离。';
export const LOCAL_BACKEND_NTFS_NOTICE =
  'Windows 上 local 另存在 NTFS 硬链接逃逸风险（工作区内恶意内容可逃逸到宿主），见 #1048。';

/** #1644 AC2：创建失败是否属于「显式选 docker 但不可用、且 local 可用」的 409。
 *  形状取自后端 `sandbox_unavailable_http_error` 的 detail（`backend` +
 *  `available_backends`）；只按字段判，不解析文案。 */
function isSandboxFallbackFailure(
  failure: SubmitTaskResult,
): failure is StartSessionFailure {
  return (
    failure !== null &&
    typeof failure !== 'string' &&
    failure.status === 409 &&
    failure.backend === 'docker' &&
    failure.availableBackends.includes('local')
  );
}

interface Props {
  /** 待开任务的项目；null = 关闭。 */
  project: Project | null;
  /** 模型目录（GET /api/models）。空数组 = 端点缺席 → 模型选择器降级隐藏。 */
  models: ModelCatalogEntry[];
  /** sandbox 后端探针（GET /api/sandbox-backends）。空数组 → 运行位置隐藏。 */
  sandboxBackends: SandboxBackendEntry[];
  onOpenChange: (open: boolean) => void;
  /** 创建并启动任务。resolve `null` = 已创建（调用方关闭浮层）；
   *  否则 resolve 给用户看的原因（浮层就地显示）。字符串是旧契约；
   *  #1644 起确认面路径还会拿到结构化失败回执（409 卡片据此渲染
   *  「改用 local 重试」）。 */
  onCreateTask: (payload: StartSessionPayload) => Promise<SubmitTaskResult>;
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
  // 验收项：可选单行输入框，提交时作为结构化 criteria 字段（#1359 阶段 2）
  const [acceptance, setAcceptance] = useState('');
  const [autonomy, setAutonomy] = useState<AutonomyId>('ask');
  // 模型：'auto' = 不发 model 键（后端默认链）；其余为目录 name
  const [model, setModel] = useState<string>('auto');
  // 运行位置：'auto' = 不发 sandbox_backend 键（部署默认）；其余为后端名
  const [sandbox, setSandbox] = useState<string>('auto');
  // 失败回执：字符串（旧契约）或 #1644 的结构化 409 回执。
  const [failure, setFailure] = useState<SubmitTaskResult>(null);
  const [pending, setPending] = useState(false);
  // #1177 §B：高级设置默认折叠（任务描述是唯一主视觉）。
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [pathExpanded, setPathExpanded] = useState(false);

  const promptValid = prompt.trim().length > 0;

  /** `overrides.sandbox_backend` = 「改用 local 重试」的强制档位：重发**原 payload**，
   *  只把运行位置换成 local（#1644 AC2）。用参数而不是先 setSandbox 再 submit——
   *  后者读到的仍是旧闭包值（React 状态尚未重渲染）。 */
  const submit = async (overrides?: { sandbox_backend: string }) => {
    if (pending || !promptValid) return;
    setPending(true);
    setFailure(null);
    // 完成目标走结构化字段（#1359 阶段 2）：不再拼进 prompt——验收项进
    // task/defined.criteria，`task` 保持 prompt 原文。
    const acceptanceText = acceptance.trim();
    const sandboxBackend = overrides?.sandbox_backend ?? sandbox;
    const payload: StartSessionPayload = {
      task: prompt.trim(),
      ...(acceptanceText ? { criteria: [{ text: acceptanceText }] } : {}),
      cwd: project.path,
      autonomy,
      ...(model !== 'auto' ? { model } : {}),
      ...(sandboxBackend !== 'auto' ? { sandbox_backend: sandboxBackend } : {}),
      // on_conflict 不发 = 后端默认 worktree（选项 A：目录冲突默认自动 worktree）
    };
    const result = await onCreateTask(payload);
    if (result) {
      setFailure(result);
      setPending(false);
      return;
    }
    onOpenChange(false);
  };

  const retryWithLocal = () => {
    setSandbox('local');
    void submit({ sandbox_backend: 'local' });
  };

  const modelOptions = [
    { value: 'auto', title: 'Auto', description: '默认模型链' },
    ...models.map((m) => ({
      value: m.name,
      title: m.name,
      description: m.provider ? `via ${m.provider}` : undefined,
    })),
  ];

  // #1048 AC2：`windows_psec` 已降级为 legacy-only，不再是新会话可选后端——
  // 探针结果（props.sandboxBackends）里的它一律不在下拉展示。
  const projectBackends = sandboxBackends.filter(
    (b) => b.backend !== 'windows_psec',
  );
  const sandboxOptions = [
    { value: 'auto', title: '默认', description: '部署默认（本机，无 OS 级隔离）' },
    ...projectBackends
      .filter((b) => b.available)
      .map((b) => ({
        value: b.backend,
        title: b.backend === 'local'
          ? '本机'
          : b.backend === 'docker'
            ? 'Docker'
            : b.backend,
        // #1644（本票为文案基准，#1486 对齐本票）：local 无 OS 级隔离必须在 UI
        // 明示；通用句与 409 卡片共用同一常量。
        description: b.backend === 'local'
          ? LOCAL_BACKEND_NOTICE
          : b.backend === 'docker'
            ? '容器内运行：非 root、cap_drop=ALL、no-new-privileges'
            : undefined,
      })),
  ];
  // 不可用的后端不进选项（不画饼）；但若之前选过已变不可用的，后端会 409 结构化报错
  const unavailableBackends = projectBackends.filter((b) => !b.available);

  return (
    <Dialog.Content
      className="project-dialog"
      aria-label="新建任务"
      onOpenAutoFocus={(e) => {
        /* 事件同步期先取容器：闭包在宏任务里才执行，届时 currentTarget 已是 null。 */
        const root = e.currentTarget as HTMLElement;
        // #1694：由「新建任务…」菜单项打开时，初焦等菜单卸载后再落（见 menuDialogTiming）。
        deferMenuInitialFocus(e, () => focusFirstIn(root));
      }}
    >
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

      {/* #1177 §B：保留紧凑单行提示；完整 cwd 通过按需展开入口核验。 */}
      <div className="project-path-callout" role="group" aria-label="项目工作目录">
        <TriangleAlert size={13} aria-hidden="true" />
        <span className="project-path-callout-text">
          Agent 将直接读写该目录：
          <span className="project-path-callout-path mono">{project.path}</span>
        </span>
        <button
          type="button"
          className="project-path-callout-toggle"
          aria-expanded={pathExpanded}
          aria-controls="project-path-full"
          aria-label={pathExpanded ? '收起完整项目路径' : '查看完整项目路径'}
          onClick={() => setPathExpanded((value) => !value)}
          disabled={pending}
        >
          {pathExpanded ? '收起' : '查看路径'}
        </button>
        <code
          id="project-path-full"
          className="project-path-callout-full-path mono"
          hidden={!pathExpanded}
        >
          {project.path}
        </code>
      </div>

      {/* prompt：唯一必填项，#1177 §B 的唯一主视觉（多行文本框） */}
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

      {/* #1177 §B：高级设置折叠区，默认折叠——完成目标/自主度/模型/运行位置收进这里。 */}
      <div className="project-advanced">
        <button
          type="button"
          className="project-advanced-toggle"
          aria-expanded={advancedOpen}
          aria-controls="task-advanced"
          onClick={() => setAdvancedOpen((v) => !v)}
          disabled={pending}
        >
          <ChevronRight
            size={13}
            className={`project-advanced-chevron${advancedOpen ? ' open' : ''}`}
            aria-hidden="true"
          />
          高级设置
        </button>

        <div id="task-advanced" className="project-advanced-content" hidden={!advancedOpen}>
          {/* 完成目标：可选**单行**输入，作为结构化验收项随创建请求提交
              （#1359 阶段 2：不再拼进 prompt；#1177 §B 效果图 03 的单行形态
              与用户 2026-10-17 裁决 1A 不变）。 */}
          <div className="project-field">
            <label className="project-field-label" htmlFor="task-acceptance">
              完成目标 <span className="project-field-optional">（可选）</span>
            </label>
            <input
              id="task-acceptance"
              type="text"
              className="project-input"
              value={acceptance}
              onChange={(e) => setAcceptance(e.target.value)}
              placeholder="什么结果算完成？"
              disabled={pending}
            />
            <span className="project-field-hint">
              作为验收项随任务一起保存，不拼进任务描述。
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
          {projectBackends.length > 0 && (
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
              {sandbox === 'auto' || sandbox === 'local' ? (
                <span className="project-field-hint">
                  本机：命令以当前用户身份直接读写宿主目录、可触达本机网络，无 OS 级隔离；需要 OS 级边界请选 Docker。
                </span>
              ) : null}
              <span className="project-field-hint">
                目录被其他任务占用时，默认自动建 worktree 并行（可在创建请求里改排队）。
              </span>
            </div>
          )}
        </div>
      </div>

      {failure && (
        <div className="project-error" role="alert">
          <span className="project-error-message">
            {typeof failure === 'string' ? failure : failure.message}
          </span>
          {/* #1644 AC2：显式选 docker 而 docker 不可用、local 可用 -> 给切换动作。
              两条明示文案与运行位置 local 选项同源；按钮以原 payload 重发，
              只把 sandbox_backend 换成 local。其它失败形状不渲染这一块。 */}
          {isSandboxFallbackFailure(failure) && (
            <div className="project-error-sandbox">
              <span className="project-error-notice">{LOCAL_BACKEND_NOTICE}</span>
              <span className="project-error-notice">{LOCAL_BACKEND_NTFS_NOTICE}</span>
              <button
                type="button"
                className="project-btn project-error-retry"
                onClick={retryWithLocal}
                disabled={pending}
              >
                改用 local 重试
              </button>
            </div>
          )}
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
          {pending ? '正在创建…' : '创建并启动'}
        </button>
      </div>
    </Dialog.Content>
  );
}
