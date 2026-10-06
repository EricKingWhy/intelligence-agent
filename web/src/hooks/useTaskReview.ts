/** #353 W-09：任务审阅数据 hook（服务端投影驱动，不变量 #22）。
 *
 *  唯一的业务事实来源是服务端两个投影端点：`GET /task`（交付四态 + 验收项 +
 *  验证值 + 版本 + 在途 run）与 `GET /evidence`（逐条证据 + 服务端 `freshness`）。
 *  本 hook **不合并** `product_state` 与 `freshness`、**不推断** UNKNOWN、**不写**
 *  localStorage / 任何客户端缓存——它只负责"拉取、透传、重拉"。
 *
 *  重拉时机（对齐既有 `lib/reconnect.ts` 的纪律：迟到/重连从当前权威状态重建）：
 *  挂载 / 换会话 / 窗口隐藏再显示（visibilitychange）/ 浏览器断线重连（online）。
 *  `refresh()` 供操作成功后就地对账。
 *
 *  三操作（各自后果见组件文案，互不共用）：
 *  - `accept` / `acceptWithGaps`：CAS 裁决（`expected_version` 取自当前投影）；
 *    409 不靠字符串猜——重拉一次投影，若已接受则按幂等成功，否则如实报冲突。
 *  - `releaseAcceptance`：撤销裁决（同样 CAS）。
 *  - `releaseLease`：释放**目录写租约**（幂等；`released:false` = 非持有者，如实显示）。 */

import { useCallback, useEffect, useState } from 'react';

import {
  acceptTask,
  getEvidenceState,
  getTaskState,
  getWorkspaceGitDiff,
  getWorkspaceGitStatus,
  releaseTaskAcceptance,
  releaseTaskLease,
  TaskNotDefinedError,
  TaskReviewRequestError,
  type EvidenceByCriterion,
  type GitCommandResult,
  type TaskState,
} from '../lib/api';

export interface TaskReviewState {
  loading: boolean;
  /** 加载层的错误（网络 / 5xx）；未定义任务**不是**错误（走 `notDefined`）。 */
  error: string | null;
  /** 404 = 会话尚无任务定义（空态，不伪装成错误）。 */
  notDefined: boolean;
  task: TaskState | null;
  evidence: EvidenceByCriterion;
  /** P1-1：证据加载失败的如实记录（不可得≠缺证据）；null = 证据可用。 */
  evidenceError: string | null;
  gitStatus: GitCommandResult | null;
  gitDiff: GitCommandResult | null;
  refresh: () => Promise<void>;
  busy: boolean;
  operationError: string | null;
  /** 裁决操作的后果回执（不是"成功"的业务结论，只是服务端已受理 + 最新投影）。 */
  operationNotice: string | null;
  /** 释放目录租约的回执（与接受/撤销接受分开，语义不同）。 */
  leaseNotice: string | null;
  accept: () => Promise<void>;
  acceptWithGaps: (reason: string) => Promise<void>;
  releaseAcceptance: () => Promise<void>;
  releaseLease: () => Promise<void>;
}

export function useTaskReview(sessionId: string | null): TaskReviewState {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notDefined, setNotDefined] = useState(false);
  const [task, setTask] = useState<TaskState | null>(null);
  const [evidence, setEvidence] = useState<EvidenceByCriterion>({});
  // P1-1：证据加载失败必须如实呈现，不能静默吞成 {}（否则面板会把"不可得"渲染成"缺证据"）。
  const [evidenceError, setEvidenceError] = useState<string | null>(null);
  const [gitStatus, setGitStatus] = useState<GitCommandResult | null>(null);
  const [gitDiff, setGitDiff] = useState<GitCommandResult | null>(null);

  const [busy, setBusy] = useState(false);
  const [operationError, setOperationError] = useState<string | null>(null);
  const [operationNotice, setOperationNotice] = useState<string | null>(null);
  const [leaseNotice, setLeaseNotice] = useState<string | null>(null);

  const clearTransient = useCallback(() => {
    setOperationError(null);
    setOperationNotice(null);
    setLeaseNotice(null);
  }, []);

  const load = useCallback(async () => {
    if (!sessionId) {
      setTask(null);
      setEvidence({});
      setEvidenceError(null);
      setGitStatus(null);
      setGitDiff(null);
      setNotDefined(false);
      setError(null);
      return;
    }
    setLoading(true);
    try {
      const nextTask = await getTaskState(sessionId);
      setNotDefined(false);
      setTask(nextTask);
      setError(null);
      // 证据与工作区 git 是 best-effort：拿不到不把整个面板判成加载失败，
      // 但证据失败必须记下来，各自区块如实呈现"不可得"，不能渲染成"缺证据"。
      let evidenceErr: string | null = null;
      const [nextEvidence, status, diff] = await Promise.all([
        getEvidenceState(sessionId).catch((e) => {
          evidenceErr = e instanceof Error ? e.message : '证据加载失败';
          return {} as EvidenceByCriterion;
        }),
        getWorkspaceGitStatus(sessionId).catch(() => null),
        getWorkspaceGitDiff(sessionId).catch(() => null),
      ]);
      setEvidence(nextEvidence);
      setEvidenceError(evidenceErr);
      setGitStatus(status);
      setGitDiff(diff);
    } catch (e) {
      if (e instanceof TaskNotDefinedError) {
        setNotDefined(true);
        setTask(null);
        setEvidence({});
        setEvidenceError(null);
        setGitStatus(null);
        setGitDiff(null);
        setError(null);
      } else {
        setError(e instanceof Error ? e.message : '加载任务状态失败');
      }
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  // 重拉触发：窗口隐藏再显示 / 断线重连。都是"从当前权威状态重建"，不合并本地态。
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void load();
    };
    const onOnline = () => void load();
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('online', onOnline);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('online', onOnline);
    };
  }, [load]);

  /** 裁决提交（accept / acceptWithGaps 共用）。409 用最新投影判定语义，不猜字符串。 */
  const submitAcceptance = useCallback(
    async (decision: 'accepted' | 'accepted_with_gaps', reason?: string) => {
      if (!sessionId || !task) return;
      setBusy(true);
      clearTransient();
      try {
        const updated = await acceptTask(sessionId, {
          decision,
          ...(reason !== undefined ? { reason } : {}),
          expected_version: task.version,
        });
        if (updated && updated.defined) setTask(updated);
        setOperationNotice(
          decision === 'accepted'
            ? '已接受：服务端交付状态为「已接受」。'
            : '已带缺项接受：服务端交付状态为「已接受」，并记录了带缺项原因。',
        );
        await load();
      } catch (e) {
        if (e instanceof TaskReviewRequestError && e.status === 409) {
          // 409 有两种来源（已接受过 / 版本冲突）——重拉权威投影来区分；若已接受则
          // 用户的裁决意图已生效，按幂等成功显示，不弹吓人错误。
          try {
            const fresh = await getTaskState(sessionId);
            if (fresh.acceptance !== null) {
              setTask(fresh);
              setOperationNotice('已接受（此前已完成，重复请求按幂等处理）。');
              return;
            }
          } catch {
            /* 重拉失败：落到下面的如实报错分支。 */
          }
          setOperationError(e.message);
        } else {
          setOperationError(e instanceof Error ? e.message : '接受任务失败');
        }
      } finally {
        setBusy(false);
      }
    },
    [sessionId, task, load, clearTransient],
  );

  const accept = useCallback(() => submitAcceptance('accepted'), [submitAcceptance]);

  const acceptWithGaps = useCallback(
    async (reason: string) => {
      // 客户端先校验（票面：accepted_with_gaps 必带 reason）；服务端 422 仍如实展示。
      if (!reason.trim()) {
        clearTransient();
        setOperationError('带原因接受必须填写原因。');
        return;
      }
      await submitAcceptance('accepted_with_gaps', reason);
    },
    [submitAcceptance, clearTransient],
  );

  const releaseAcceptance = useCallback(async () => {
    if (!sessionId || !task) return;
    setBusy(true);
    clearTransient();
    try {
      const updated = await releaseTaskAcceptance(sessionId, { expected_version: task.version });
      if (updated && updated.defined) setTask(updated);
      setOperationNotice('已撤销接受：接受裁决已释放，验收项的验证值保持不变。');
      await load();
    } catch (e) {
      setOperationError(e instanceof Error ? e.message : '撤销接受失败');
    } finally {
      setBusy(false);
    }
  }, [sessionId, task, load, clearTransient]);

  const releaseLease = useCallback(async () => {
    if (!sessionId) return;
    setBusy(true);
    clearTransient();
    try {
      const result = await releaseTaskLease(sessionId);
      setLeaseNotice(
        result.released
          ? `已释放目录写租约${result.promoted_to ? `；队首 ${result.promoted_to} 提升为持有者` : ''}。`
          : '未释放：本会话不是该目录的写租约持有者（released:false）。',
      );
    } catch (e) {
      setOperationError(e instanceof Error ? e.message : '释放目录失败');
    } finally {
      setBusy(false);
    }
  }, [sessionId, clearTransient]);

  return {
    loading,
    error,
    notDefined,
    task,
    evidence,
    evidenceError,
    gitStatus,
    gitDiff,
    refresh: load,
    busy,
    operationError,
    operationNotice,
    leaseNotice,
    accept,
    acceptWithGaps,
    releaseAcceptance,
    releaseLease,
  };
}
