/** useSession — orchestrates session list, history loading, and live streaming.
 *
 * Single React hook that components consume. Internally:
 *   - loads session list on mount
 *   - when a session is selected, loads its durable events → projects to ConversationState
 *   - on new task submit, POSTs and consumes SSE → folds events into same state
 *
 * Disconnect cleanup: SSE handle's cancel() is wired to unmount via useEffect ref.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { AgentEvent, ConversationState, SessionSummary } from '../types';
import { listSessions, getSessionEvents, startSession, type StartSessionPayload } from '../lib/api';
import { consumeSSE, type SSEHandle } from '../lib/sse';
import { initConversation, applyEvent, projectHistory } from '../lib/projection';

export function useSession() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [conversation, setConversation] = useState<ConversationState | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const sseRef = useRef<SSEHandle | null>(null);

  // Load session list on mount.
  const refreshSessions = useCallback(async () => {
    try {
      const list = await listSessions();
      setSessions(list);
    } catch (e) {
      setError(`Failed to load sessions: ${(e as Error).message}`);
    }
  }, []);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  // Load history when a session is selected.
  useEffect(() => {
    if (!selectedId) {
      setConversation(null);
      return;
    }
    let cancelled = false;
    setLoadingHistory(true);
    setError(null);
    getSessionEvents(selectedId)
      .then((events: AgentEvent[]) => {
        if (cancelled) return;
        setConversation(projectHistory(selectedId, events));
      })
      .catch((e) => {
        if (!cancelled) setError(`Failed to load events: ${(e as Error).message}`);
      })
      .finally(() => {
        if (!cancelled) setLoadingHistory(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  // Cleanup SSE on unmount.
  useEffect(() => {
    return () => {
      sseRef.current?.cancel();
    };
  }, []);

  /** Submit a new task. Creates a fresh session and streams the response. */
  const submitTask = useCallback(
    async (payload: StartSessionPayload) => {
      setError(null);
      setStreaming(true);
      try {
        const res = await startSession(payload);
        if (!res.ok || !res.body) {
          throw new Error(`Start failed: ${res.status}`);
        }

        // We don't know session_id up-front from SSE (events carry it), so init blank
        // and fill from first event. For projection safety, use a temp id then patch.
        let conv: ConversationState | null = conversation;

        const handle = consumeSSE(
          res,
          (event: AgentEvent) => {
            if (!conv) {
              // First event seeds the conversation.
              const sid = (event.data.session_id as string) ?? 'streaming';
              conv = initConversation(sid);
            }
            conv = applyEvent(conv, event);
            setConversation({ ...conv });
            // Once we have a session_id, select it so list reload includes it.
            const sid = (event.data.session_id as string) ?? conv.session_id;
            if (sid && sid !== 'streaming' && selectedId !== sid) {
              setSelectedId(sid);
            }
          },
          () => {
            setStreaming(false);
            refreshSessions();
          },
          (err) => {
            setStreaming(false);
            setError(`Stream error: ${(err as Error).message}`);
          },
        );
        sseRef.current = handle;
      } catch (e) {
        setStreaming(false);
        setError(`Submit failed: ${(e as Error).message}`);
      }
    },
    [conversation, selectedId, refreshSessions],
  );

  const cancelStream = useCallback(() => {
    sseRef.current?.cancel();
    sseRef.current = null;
    setStreaming(false);
  }, []);

  return {
    sessions,
    selectedId,
    conversation,
    loadingHistory,
    streaming,
    error,
    selectSession: setSelectedId,
    submitTask,
    cancelStream,
    refreshSessions,
  };
}
