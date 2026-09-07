/** Composer — task input at the bottom of the conversation column.
 *
 * Submits on Cmd/Ctrl+Enter. Disabled while streaming.
 * presetTask: 外部注入的示例任务（空状态 chip 点击），注入后仍可自由编辑。
 */

import { memo, useEffect, useState, type KeyboardEvent } from 'react';
import { ArrowUp, Brain, Shield, Square, User } from 'lucide-react';
import type { PresetTask } from '../types';
import type { CatalogEntry, ModelCatalogEntry } from '../lib/api';
import { ModelPicker } from './ModelPicker';
import { ControlPicker } from './ControlPicker';

interface Props {
  streaming: boolean;
  onSubmit: (task: string) => void;
  onCancel: () => void;
  presetTask?: PresetTask | null;
  /** T10 #103 模型目录（GET /api/models）：空 = 端点缺席/解析失败 → 选择器
   *  降级隐藏（不伪造列表）。条目即目录真相，零硬编码模型名。 */
  models?: ModelCatalogEntry[];
  /** 当前选中（null = 默认链，提交不带 model 字段）。 */
  selectedModel?: string | null;
  onModelChange?: (name: string | null) => void;
  // ── Phase 2b Composer control row（Ticket F1）──
  /** GET /api/permission-modes 清单。空 → 隐藏控件。 */
  permissionModes?: CatalogEntry[];
  selectedPermissionMode?: string | null;
  onPermissionModeChange?: (id: string | null) => void;
  /** GET /api/agent-profiles 清单。空 → 隐藏控件。 */
  agentProfiles?: CatalogEntry[];
  selectedAgentProfile?: string | null;
  onAgentProfileChange?: (id: string | null) => void;
  /** GET /api/reasoning-efforts 清单。空 → 隐藏控件。 */
  reasoningEfforts?: CatalogEntry[];
  selectedReasoningEffort?: string | null;
  onReasoningEffortChange?: (id: string | null) => void;
  /** GET /api/context-providers 清单。空 → 隐藏控件。
   *  多选语义推迟——当前后端诚实返空，控件不会渲染。 */
  contextProviders?: CatalogEntry[];
}

// memo：流式期间 props 稳定（streaming 布尔不变、回调由 App useCallback 固定），
// 输入框不随对话区每个 delta 重渲染。
export const Composer = memo(function Composer({
  streaming,
  onSubmit,
  onCancel,
  presetTask,
  models = [],
  selectedModel = null,
  onModelChange,
  permissionModes = [],
  selectedPermissionMode = null,
  onPermissionModeChange,
  agentProfiles = [],
  selectedAgentProfile = null,
  onAgentProfileChange,
  reasoningEfforts = [],
  selectedReasoningEffort = null,
  onReasoningEffortChange,
  contextProviders = [],
}: Props) {
  const [value, setValue] = useState('');

  // 外部示例任务注入（引用变化即触发；每次点击 chip 生成新对象）
  useEffect(() => {
    if (presetTask) setValue(presetTask.text);
  }, [presetTask]);

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed || streaming) return;
    onSubmit(trimmed);
    setValue('');
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
    }
  };

  // 控件行是否渲染——至少有一个非空目录时才显示 control row 容器
  const hasControls =
    models.length > 0 ||
    permissionModes.length > 0 ||
    agentProfiles.length > 0 ||
    reasoningEfforts.length > 0 ||
    contextProviders.length > 0;

  return (
    <div className="composer-wrap">
      <div className="composer-dock surface-floating">
        <textarea
          id="composer-input"
          name="task"
          className="composer"
          placeholder="描述一个任务…（⌘+Enter 发送）"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          rows={2}
          disabled={streaming}
          aria-label="Agent 任务"
        />
        {hasControls && (
          <div className="composer-controls">
            <ModelPicker
              models={models}
              selectedModel={selectedModel}
              onModelChange={onModelChange ?? (() => {})}
              disabled={streaming}
            />
            <ControlPicker
              ariaLabel="权限模式"
              entries={permissionModes}
              selectedId={selectedPermissionMode}
              onChange={onPermissionModeChange ?? (() => {})}
              icon={Shield}
              placeholder="权限"
              disabled={streaming}
            />
            <ControlPicker
              ariaLabel="Agent Profile"
              entries={agentProfiles}
              selectedId={selectedAgentProfile}
              onChange={onAgentProfileChange ?? (() => {})}
              icon={User}
              placeholder="Agent"
              disabled={streaming}
            />
            <ControlPicker
              ariaLabel="Reasoning Effort"
              entries={reasoningEfforts}
              selectedId={selectedReasoningEffort}
              onChange={onReasoningEffortChange ?? (() => {})}
              icon={Brain}
              placeholder="推理"
              disabled={streaming}
            />
            {/* Context Providers 推迟——多选语义需要新组件或扩展 ControlPicker
                支持 multi 模式。后端当前诚实返空，此控件不会渲染。提交链路已就绪：
                App.tsx 的 handleSubmit 在 selectedContextProviders 非空时传
                context_providers 数组。 */}
          </div>
        )}
        {streaming ? (
          <>
            {/* Esc 中断提示（Claude Code "esc to interrupt" 语言）：键位绑定在 App 全局，这里只做可见性 */}
            <span className="composer-esc-hint" aria-hidden="true">
              <kbd>Esc</kbd> 停止
            </span>
            <button className="composer-stop" onClick={onCancel} aria-label="停止" title="停止">
              <Square size={14} />
            </button>
          </>
        ) : (
          <button
            className="composer-send"
            onClick={submit}
            disabled={!value.trim()}
            aria-label="发送"
            title="发送（⌘+Enter）"
          >
            <ArrowUp size={16} />
          </button>
        )}
      </div>
    </div>
  );
});
