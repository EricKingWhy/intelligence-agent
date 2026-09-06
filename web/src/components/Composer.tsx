/** Composer — task input at the bottom of the conversation column.
 *
 * Submits on Cmd/Ctrl+Enter. Disabled while streaming.
 * presetTask: 外部注入的示例任务（空状态 chip 点击），注入后仍可自由编辑。
 */

import { memo, useEffect, useState, type KeyboardEvent } from 'react';
import { ArrowUp, Square } from 'lucide-react';
import type { PresetTask } from '../types';
import type { ModelCatalogEntry } from '../lib/api';

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

  // T10 #103：选择项变化 → null（空串 = 默认链）或目录 name
  const onModelSelect = (e: { target: { value: string } }) => {
    onModelChange?.(e.target.value === '' ? null : e.target.value);
  };

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
        {/* 模型选择器（T10 #103）：目录来自端点，缺席即隐藏；选中随提交发送，
            空值 = 默认链（行为不变）。会话内实际模型仍以模型卡 data.model 真相
            展示——此处只是提交偏好，不是第二真相。 */}
        {models.length > 0 && (
          <select
            className="composer-model"
            value={selectedModel ?? ''}
            onChange={onModelSelect}
            disabled={streaming}
            aria-label="模型选择"
            title={selectedModel ?? '默认链'}
          >
            <option value="">默认链</option>
            {models.map((m) => (
              <option key={m.name} value={m.name}>
                {m.name}
                {m.default ? '（默认）' : ''}
              </option>
            ))}
          </select>
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
