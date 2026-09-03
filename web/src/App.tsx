/** App — three-column shell wired to useSession hook.
 *
 * Layout (per MASTER.md concession chain):
 *   - ≥1440px: sessions (240px) | conversation (flex) | step-detail (340px)
 *   - ≤1024px: hide step-detail
 *   - ≤768px:  sessions → 56px rail (icons only)
 *   - ≤375px:  single column (mobile degrades gracefully)
 *
 * Panel geometry is transient — NOT persisted (invariant #22: no second truth).
 */

import { useState } from 'react';
import { useSession } from './hooks/useSession';
import { TopBar } from './components/TopBar';
import { SessionList } from './components/SessionList';
import { Conversation } from './components/Conversation';
import { Composer } from './components/Composer';
import { StepDetail } from './components/StepDetail';
import type { ToolCall } from './types';
import './styles/app.css';

export default function App() {
  const {
    sessions,
    selectedId,
    conversation,
    loadingHistory,
    streaming,
    error,
    selectSession,
    submitTask,
    cancelStream,
  } = useSession();

  const [selectedTool, setSelectedTool] = useState<ToolCall | null>(null);

  const handleNew = () => {
    selectSession(null);
    setSelectedTool(null);
  };

  const handleSubmit = (task: string) => {
    setSelectedTool(null);
    void submitTask({ task, max_steps: 10, auto_approve: true });
  };

  return (
    <div className="app-shell">
      <TopBar
        sessionMeta={
          conversation
            ? { session_id: conversation.session_id, event_count: conversation.turns.length }
            : undefined
        }
        streaming={streaming}
      />

      <main className="app-columns">
        <SessionList
          sessions={sessions}
          selectedId={selectedId}
          onSelect={(id) => {
            selectSession(id);
            setSelectedTool(null);
          }}
          onNew={handleNew}
        />

        <section className="app-conversation-col surface-raised">
          {error && <div className="app-error">{error}</div>}
          <Conversation conversation={conversation} loadingHistory={loadingHistory} />
          <Composer streaming={streaming} onSubmit={handleSubmit} onCancel={cancelStream} />
        </section>

        <StepDetail
          conversation={conversation}
          selectedTool={selectedTool}
          onSelectTool={setSelectedTool}
        />
      </main>
    </div>
  );
}
