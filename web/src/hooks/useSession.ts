/** useSession — orchestrates session list, history loading, and live streaming.
 *
 * State model: a single discriminated `mode` (SessionMode) is the source of truth —
 *   idle                    → no session (empty state), conversation is null
 *   live(sessionId?)        → a new task is streaming; events paint the conversation
 *   viewing(sessionId)      → a durable session is being viewed (history rebuild)
 * `selectedId` / `streaming` are derived from mode, so conversation.session_id and
 * the highlighted row can never disagree (invariant #22: no second truth).
 *
 * Migrations:
 *   submit    → live(null), conversation reset (a new task never folds into the
 *               previously viewed session's state)
 *   first SSE frame with session_id → live(sid)
 *   done/error/cancel → viewing(known sid) or idle (nothing durable yet) — the
 *               history loader re-reads the durable log so the view always
 *               comes from the fact source.
 *   selectSession → cancels any live stream (idempotent) and switches mode.
 *
 * Disconnect cleanup: SSE handle's cancel() is wired to unmount via useEffect ref.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import type { AgentEvent, ConversationState, SessionMode, SessionSummary } from '../types';
import { EventType } from '../types';
import { listSessions, getSessionEvents, startSession, recoverSession, RecoverError, type StartSessionPayload } from '../lib/api';
import { consumeSSE, type SSEHandle } from '../lib/sse';
import { initConversation, applyEvent, projectHistory, deriveSessionTitle, extractSessionTitle } from '../lib/projection';

/** 流式帧 vs 当前模式一致性判别（不变量 #22：UI 不维护第二套真相）。
 *
 * SSE 流在 submitTask 时开启，回调闭包持有当时的 conv。若用户在流结束前
 * cancel / 切换会话 / 触发 error，mode 迁移到 viewing/idle，但 SSE 在途帧
 * 仍可能晚于这次 mode 变更到达——回调再写 conversation 就会用旧流残留
 * 覆盖刚加载的目标会话视图。
 *
 * 守护：每帧应用前先问「当前模式是否仍是这条流的权威消费者」。提取为
 * 纯函数是为了锁不变量——React 状态机本身的回归需 testing-library，
 * 引入它会扩大 scope；这一层契约纯函数就能锁住。 */
export function shouldApplyStreamFrame(mode: SessionMode, event: AgentEvent): boolean {
  // 离开 live 即丧失权威——viewing 由持久事件源重建，idle 无会话。
  if (mode.kind !== 'live') return false;
  // 首帧尚未确定 sid（sessionId === null）：任意帧都接受。
  if (mode.sessionId === null) return true;
  // 帧未带 sid（理论只会在首帧前出现）：宽容接受，避免误丢首帧。
  const eventSid = event.session_id ?? null;
  if (eventSid === null) return true;
  // sid 不匹配——旧流迟到帧窜入新会话，明确拒绝。
  return mode.sessionId === eventSid;
}

/** Recover 入口的三态视图状态（200 成功回到 idle——重建视图即成功反馈）。 */
export interface RecoverState {
  status: 'idle' | 'pending' | 'error';
  /** 409 裁决原因 / 404·网络错误的具体信息。 */
  message: string | null;
  /** 409 = 存在需人工裁决的高风险操作（展示态，非普通失败）。 */
  conflict: boolean;
}

/** Recover 三态的 idle 初值——三处复用（useState 初值 / mode 迁移重置 / 200
 *  成功回位）。对象只被整体替换、从不就地修改，共享引用安全。 */
const RECOVER_IDLE: RecoverState = { status: 'idle', message: null, conflict: false };

/** Recover 响应落地守护（不变量 #22，shouldApplyStreamFrame 的姊妹契约）。
 *
 * recover 是异步请求：pending 期间用户可能已切走（selectSession /
 * submitTask / cancelStream 都会迁移 mode）。晚到的 200 响应若仍
 * setConversation，会用旧会话的重建结果覆盖刚加载的目标会话视图——
 * 与迟到 SSE 帧同族的 stale-write。仅当当前模式仍是「查看该会话」时，
 * recover 的结果才是当前视图的权威真相。 */
export function shouldApplyRecoverResult(mode: SessionMode, sid: string): boolean {
  return mode.kind === 'viewing' && mode.sessionId === sid;
}

/** P1-3 delta 合帧提交器（HANDOFF_PERF_FRONTEND §6）：~24ms 窗口内多个 delta
 * 只触发一次 React 提交。语义边界——只合并「提交」，不合并「折叠」：每帧仍
 * 逐帧过 shouldApplyStreamFrame 守护并立即 applyEvent 进本地 conv（真相不
 * 延迟）；延迟的只是 setConversation 通知。流终止（run/completed、
 * run/failed、onDone、onError）必须 flush，否则尾帧丢失。 */
export interface CommitCoalescer {
  /** 标记有待提交数据；窗口内多次调用只调度一次。 */
  schedule(): void;
  /** 立即提交待合帧数据（无则 no-op），并取消挂起的定时器。 */
  flush(): void;
  /** 丢弃挂起的定时器与待提交标记（流被 cancel 后不再迟到提交）。 */
  cancel(): void;
}

export function createCommitCoalescer(submit: () => void, windowMs = 24): CommitCoalescer {
  let dirty = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const fire = () => {
    timer = null;
    if (!dirty) return;
    dirty = false;
    submit();
  };
  return {
    schedule() {
      dirty = true;
      if (timer === null) timer = setTimeout(fire, windowMs);
    },
    flush() {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      fire();
    },
    cancel() {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      dirty = false;
    },
  };
}

export function useSession() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [mode, setMode] = useState<SessionMode>({ kind: 'idle' });
  const [conversation, setConversation] = useState<ConversationState | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Session Rail 行标题缓存（Session Model E 轮）：查看某会话时从首条 user/message
  // 投影出标题，本地缓存供 SessionList 行渲染。无缓存时回退到短 ID——不伪造。
  const [titlesById, setTitlesById] = useState<Record<string, string>>({});

  const sseRef = useRef<SSEHandle | null>(null);
  // live 流中已知的首帧 sid——结束/出错/取消时决定迁移目标。
  const liveSidRef = useRef<string | null>(null);
  // mode 的实时镜像——SSE 回调闭包在 submitTask 时定型，读到的 mode 是
  // 提交瞬间快照而非当前真相。镜像让每帧应用前能问「当前模式是否仍是
  // 这条流的权威消费者」（不变量 #22），从而拒绝 cancel / selectSession
  // 之后才到达的迟到帧。
  const modeRef = useRef<SessionMode>(mode);
  modeRef.current = mode;

  // Recover 三态（df4f7d8 §1.1）：idle → pending → 200 成功（回到 idle，整表
  // 重建）/ 404·409·网络错误 → error。409 是"需人工裁决"（conflict=true），
  // 本期只展示原因，不做裁决交互（不变量 #14：不伪造、不盲跑）。
  const [recoverState, setRecoverState] = useState<RecoverState>(RECOVER_IDLE);

  // Derived, so the UI can never observe a mismatch between them.
  const selectedId = mode.kind === 'idle' ? null : mode.sessionId;
  const streaming = mode.kind === 'live';

  // Load session list on mount.
  const refreshSessions = useCallback(async () => {
    try {
      const list = await listSessions();
      setSessions(list);
      // 后端 Gap 3：列表 payload 携带首条用户消息（截断 128）——零额外请求预填
      // 标题缓存。events 扫描（viewing 路径）保留为后端未返回时的 fallback；
      // 已有标题不覆盖（事件派生值优先，保持单一更新路径语义）。
      setTitlesById((m) => {
        let changed = false;
        const next = { ...m };
        for (const s of list) {
          if (s.first_user_message && !next[s.session_id]) {
            next[s.session_id] = s.first_user_message;
            changed = true;
          }
        }
        return changed ? next : m;
      });
    } catch (e) {
      setError(`加载会话列表失败：${(e as Error).message}`);
    }
  }, []);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  // History loading: viewing mode reads the durable log; live mode lets the
  // stream paint (a stale/partial disk read would overwrite in-flight state);
  // idle owns no conversation.
  useEffect(() => {
    // 任何 mode 迁移都重置 recover 三态（恢复状态不跨会话/流存活）。
    setRecoverState(RECOVER_IDLE);
    if (mode.kind === 'idle') {
      setConversation(null);
      return;
    }
    if (mode.kind !== 'viewing') return;
    const sid = mode.sessionId;
    let cancelled = false;
    setLoadingHistory(true);
    setError(null);
    getSessionEvents(sid)
      .then((events: AgentEvent[]) => {
        if (cancelled) return;
        setConversation(projectHistory(sid, events));
        // 从真实事件流投影首条用户消息作为行标题（无则空串，回退短 ID）。
        const title = deriveSessionTitle(events);
        if (title) setTitlesById((m) => (m[sid] === title ? m : { ...m, [sid]: title }));
      })
      .catch((e) => {
        if (!cancelled) setError(`加载历史事件失败：${(e as Error).message}`);
      })
      .finally(() => {
        if (!cancelled) setLoadingHistory(false);
      });
    return () => {
      cancelled = true;
    };
  }, [mode]);

  // Cleanup SSE on unmount.
  useEffect(() => {
    return () => {
      sseRef.current?.cancel();
    };
  }, []);

  /** Submit a new task. Creates a fresh session and streams the response.
   *  The conversation is reset first — a live stream never folds into the
   *  previously viewed session's turns. */
  const submitTask = useCallback(
    async (payload: StartSessionPayload) => {
      setError(null);
      setConversation(null);
      liveSidRef.current = null;
      setMode({ kind: 'live', sessionId: null });
      try {
        const res = await startSession(payload);
        if (!res.ok || !res.body) {
          throw new Error(`Start failed: ${res.status}`);
        }

        let conv: ConversationState | null = null;
        // P1-3 合帧：折叠逐帧即时（真相不延迟），提交按 ~24ms 窗口合并。
        // submit 守卫 mode 仍是 live——cancel/selectSession 之后的迟到 fire
        // 不得把旧流残留写回视图（与 shouldApplyStreamFrame 同一族守护）。
        const coalescer = createCommitCoalescer(() => {
          if (conv && modeRef.current.kind === 'live') setConversation({ ...conv });
        });

        const handle = consumeSSE(
          res,
          (event: AgentEvent) => {
            // 不变量 #22 守护：cancel / selectSession / error 之后才到达的
            // 在途帧不再具有权威——若放行会用旧流残留覆盖刚加载的目标视图。
            if (!shouldApplyStreamFrame(modeRef.current, event)) return;
            const sid = event.session_id ?? null;
            if (sid) {
              liveSidRef.current = sid;
              // 首帧确认：把 sid 写进 mode（仅从 null 迁移一次）
              setMode((m) =>
                m.kind === 'live' && m.sessionId === null ? { kind: 'live', sessionId: sid } : m,
              );
            }
            if (!conv) conv = initConversation(sid ?? 'streaming');
            conv = applyEvent(conv, event);
            // 首条用户消息到达时缓存行标题（Session Model E 轮）。
            if (sid) {
              const content = extractSessionTitle(event);
              if (content) setTitlesById((m) => (m[sid] ? m : { ...m, [sid]: content }));
            }
            // 终态事件立即 flush（尾帧不得延迟到下一窗口）；中间帧合帧提交。
            if (event.type === EventType.RUN_COMPLETED || event.type === EventType.RUN_FAILED) {
              coalescer.flush();
            } else {
              coalescer.schedule();
            }
          },
          () => {
            // Stream finished: view the session it produced (history loader
            // re-reads the durable log), and refresh the list for the new row.
            coalescer.flush(); // 尾帧不丢（P1-3）
            const sid = liveSidRef.current;
            setMode(sid ? { kind: 'viewing', sessionId: sid } : { kind: 'idle' });
            refreshSessions();
          },
          (err) => {
            coalescer.flush(); // 尾帧不丢（P1-3）
            const sid = liveSidRef.current;
            setMode(sid ? { kind: 'viewing', sessionId: sid } : { kind: 'idle' });
            setError(`流式错误：${(err as Error).message}`);
          },
        );
        sseRef.current = handle;
      } catch (e) {
        setMode({ kind: 'idle' });
        setError(`提交失败：${(e as Error).message}`);
      }
    },
    [refreshSessions],
  );

  /** Abort the live stream, if any. Falls back to the durable facts already
   *  on disk (viewing(sid)) — or idle when the stream never identified itself. */
  const cancelStream = useCallback(() => {
    sseRef.current?.cancel();
    sseRef.current = null;
    setMode(
      liveSidRef.current
        ? { kind: 'viewing', sessionId: liveSidRef.current }
        : { kind: 'idle' },
    );
  }, []);

  /** Switch sessions. Cancels any live stream first (idempotent, no-op when idle) —
   *  the UI only ever presents the session the mode points at. */
  const selectSession = useCallback((id: string | null) => {
    sseRef.current?.cancel();
    sseRef.current = null;
    liveSidRef.current = null;
    setMode(id ? { kind: 'viewing', sessionId: id } : { kind: 'idle' });
  }, []);

  /** 恢复中断会话（POST /recover，幂等）。200 → 整表重建：响应是与 GET events
   *  同构的全量事件数组，走同一 projectHistory 管线（不变量 #22——不引入第二套
   *  会话真相）；404/409 → 三态 error（409 附裁决原因，conflict=true）。
   *  落地前先过 shouldApplyRecoverResult 守护：pending 期间切走即丢弃。 */
  const recover = useCallback(
    async (sid: string) => {
      setRecoverState({ status: 'pending', message: null, conflict: false });
      try {
        const events = await recoverSession(sid);
        // stale-write 守护（不变量 #22）：pending 期间用户可能已切走——
        // 晚到的 200 响应不得覆盖目标会话视图（viewing 会由持久事件源重建）。
        if (shouldApplyRecoverResult(modeRef.current, sid)) {
          setConversation(projectHistory(sid, events));
        }
        setRecoverState(RECOVER_IDLE);
        void refreshSessions();
      } catch (e) {
        if (e instanceof RecoverError) {
          setRecoverState({
            status: 'error',
            message: e.message,
            conflict: e.status === 409,
          });
        } else {
          setRecoverState({ status: 'error', message: (e as Error).message, conflict: false });
        }
      }
    },
    [refreshSessions],
  );

  return {
    sessions,
    selectedId,
    conversation,
    loadingHistory,
    streaming,
    error,
    titlesById,
    recoverState,
    selectSession,
    submitTask,
    cancelStream,
    recover,
    refreshSessions,
  };
}
