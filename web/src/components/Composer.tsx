/** Composer — task input at the bottom of the conversation column.
 *
 * Submits on Cmd/Ctrl+Enter. Disabled while streaming.
 * presetTask: 外部注入的示例任务（空状态 chip 点击），注入后仍可自由编辑。
 */

import { useEffect, useState, type KeyboardEvent } from 'react';
import { ArrowUp, Square } from 'lucide-react';

interface Props {
  streaming: boolean;
  onSubmit: (task: string) => void;
  onCancel: () => void;
  presetTask?: { text: string; id: number } | null;
}

export function Composer({ streaming, onSubmit, onCancel, presetTask }: Props) {
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
        {streaming ? (
          <button className="composer-stop" onClick={onCancel} aria-label="停止" title="停止">
            <Square size={12} />
          </button>
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
}
