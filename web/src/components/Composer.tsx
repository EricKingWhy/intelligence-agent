/** Composer — task input at the bottom of the conversation column.
 *
 * Submits on Cmd/Ctrl+Enter. Disabled while streaming.
 */

import { useState, type KeyboardEvent } from 'react';
import { Send, Square } from 'lucide-react';

interface Props {
  streaming: boolean;
  onSubmit: (task: string) => void;
  onCancel: () => void;
}

export function Composer({ streaming, onSubmit, onCancel }: Props) {
  const [value, setValue] = useState('');

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
      <textarea
        id="composer-input"
        name="task"
        className="composer glass"
        placeholder="Describe a task for the agent… (⌘+Enter to send)"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        rows={2}
        disabled={streaming}
        aria-label="Agent task"
      />
      <div className="composer-actions">
        {streaming ? (
          <button className="btn-danger" onClick={onCancel}>
            <Square size={14} /> Stop
          </button>
        ) : (
          <button className="btn-primary" onClick={submit} disabled={!value.trim()}>
            <Send size={14} /> Send
          </button>
        )}
      </div>
    </div>
  );
}
