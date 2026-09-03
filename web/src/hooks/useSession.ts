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
    // Don't reload history while a live stream is painting this same session —
    // that would overwrite in-flight state with a stale/partial read from disk.
    if (streaming) return;
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
  }, [selectedId, streaming]);

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
        let liveSessionId: string | null = null;

        const handle = consumeSSE(
          res,
          (event: AgentEvent) => {
            const sid = event.session_id ?? null;
            if (sid) liveSessionId = sid;
            if (!conv) {
              conv = initConversation(sid ?? 'streaming');
            }
            conv = applyEvent(conv, event);
            setConversation({ ...conv });
          },
          () => {
            setStreaming(false);
            // Stream finished: persist the live session as selected so the history
            // loader runs (and list refresh picks up the new session row).
            if (liveSessionId) setSelectedId(liveSessionId);
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
    [conversation, refreshSessions],
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
