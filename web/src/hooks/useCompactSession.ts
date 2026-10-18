/** useCompactSession — 手动压缩的三态状态机（#635 / #1197 契约的唯一宿主）。
 *
 *  #1197 三态契约（供 ContextUsagePanel 与 #1195 slash `/compact` 共用——审查
 *  A-P1-3：两条入口必须同一份逻辑，不许各写一套漂移）：
 *   - 压缩中 = 流程已启动未终态（宿主渲染"正在压缩上下文…"+ spinner，并以此
 *     禁用再次触发——本 hook 的 `run` 单飞：在途重复调用直接忽略）；
 *   - 被拒   = 200 但 `compacted_turn_count == 0`（零写入），或 409
 *     `{reason: "no_early_turns"}`（#1197 项 3 的类型化拒绝）——两者都不是错误，
 *     统一说「没有可压缩的早期内容」（与 CLI、面板低水位提示同词）；
 *   - 失败   = 传输/服务端异常（`compactError` 为后端 detail，宿主就地展示）。
 *
 *   #1325 S5/S9：`reason` 是机读成因，只在 `describeCompactError` 这一个点分派；
 *   其余 409（run_busy / event_drift / 压缩进行中）保持后端可读文案。
 *  成功回执（`compactResult`）携带 tokens_before/after 供宿主做前后对比；水位
 *  刷新由宿主经 `onSuccess` 自行决定（面板重拉水位；slash 路径无水位面可刷）。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  compactSession,
  describeSessionError,
  SessionError,
  type SessionContextCompacted,
} from '../lib/api';

/** #1325 S5/S9：后端 `reason` 是机读成因，前端在此唯一分派点映射成中文。
 *  与 CLI `compress` 的文案、面板低水位提示同词——用户不该在两条入口读到两种语言。
 *  其余 409（`run_busy` / `event_drift` / 压缩进行中）保持后端原文（票面：文案不变）。 */
const COMPACT_REJECT_COPY: Record<string, string> = {
  no_early_turns: '没有可压缩的早期内容',
};

function describeCompactError(error: unknown): string {
  if (error instanceof SessionError && error.reason !== null) {
    const mapped = COMPACT_REJECT_COPY[error.reason];
    if (mapped) return mapped;
  }
  return describeSessionError(error, '压缩失败');
}

export interface CompactRunOptions {
  /** 成功回执（含被拒=零写入）后的宿主钩子；抛错不影响三态本身。 */
  onSuccess?: (result: SessionContextCompacted) => void;
}

export function useCompactSession(sessionId: string | null) {
  const [compacting, setCompacting] = useState(false);
  const [compactResult, setCompactResult] = useState<SessionContextCompacted | null>(null);
  const [compactError, setCompactError] = useState<string | null>(null);
  // 单飞闸用 ref 同步判：同一 tick 的第二次触发读不到刚 set 的 state。
  const inFlight = useRef(false);

  // A-P2（复审）：压缩失败与 slash 反馈面同一条 A-P4 纪律——上一条结论不该常驻到
  // 下一次会话结束。8s 自动消退（与 Composer 的 slashError/slashInfo 同款时长）；
  // 挂在 hook 里而不是宿主，ContextUsagePanel 与 slash 两条入口共享同一份行为，
  // 不各写一套计时器漂移。
  useEffect(() => {
    if (compactError === null) return;
    const t = setTimeout(() => setCompactError(null), 8000);
    return () => clearTimeout(t);
  }, [compactError]);

  const run = useCallback(
    async (opts?: CompactRunOptions): Promise<void> => {
      if (sessionId === null || inFlight.current) return;
      inFlight.current = true;
      setCompacting(true);
      setCompactError(null);
      try {
        const result = await compactSession(sessionId);
        setCompactResult(result);
        opts?.onSuccess?.(result);
      } catch (e) {
        setCompactError(describeCompactError(e));
      } finally {
        inFlight.current = false;
        setCompacting(false);
      }
    },
    [sessionId],
  );

  return { compacting, compactResult, compactError, run };
}
