/** ApprovePolicyPanel — 项目级持久审批规则管理（#684 Phase 2）。
 *
 * 形态并入既有 App Bar 管理簇（与「记忆管理」「上下文容量」同族），沿用
 * MemoryPanel 的 Radix Dialog 壳与 `project-dialog-*` 样式——是工作台里的一个
 * 管理浮层，不是独立页面/路由。
 *
 * 单一真相：数据源只有 `GET /api/approve-policy/rules`（后端读项目根
 * `.agent-harness/approve-policy.json`，与执行域命中规则时读的是同一份文件）。
 * 前端只读展示 + 原样撤销，**不提供创建入口**——唯一安装路径是用户在审批卡上主动
 * 选「以后都允许」（安全红线 F21：禁止静默创建持久授权）。
 *
 * 撤销是显式二次动作（点「撤销」→ 再点「确认撤销」）：持久 always-allow 是全系统
 * 攻击面最大的单点，任何变更都必须由用户主动确认，不能一次点击就生效。
 */

import { useCallback, useEffect, useState } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { RefreshCw, ShieldCheck, Trash2, X } from 'lucide-react';
import {
  listApprovePolicyRules,
  revokeApprovePolicyRule,
  type ApprovePolicyRule,
} from '../lib/api';

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const GRANULARITY_LABELS: Record<ApprovePolicyRule['granularity'], string> = {
  exact: '精确（含参数）',
  command: '命令级（不含参数）',
};

export function ApprovePolicyPanel({ open, onOpenChange }: Props) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="palette-overlay" />
        {open && <ApprovePolicyPanelBody />}
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function ApprovePolicyPanelBody() {
  const [rules, setRules] = useState<ApprovePolicyRule[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setRules(await listApprovePolicyRules());
    } catch (e) {
      setError(`加载持久审批规则失败：${(e as Error).message}`);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const revoke = async (id: string) => {
    setBusyId(id);
    setError(null);
    setNotice(null);
    try {
      await revokeApprovePolicyRule(id);
      // 本地只移除这一条：与后端"删掉即从文件消失"同源（list 再拉也是同一真相）。
      setRules((current) => (current ? current.filter((rule) => rule.id !== id) : current));
      setNotice('已撤销该持久规则。同项目新会话将重新弹出审批。');
    } catch (e) {
      setError(`撤销失败：${(e as Error).message}`);
    } finally {
      setBusyId(null);
      setConfirmId(null);
    }
  };

  return (
    <Dialog.Content className="project-dialog approve-policy-dialog">
      <div className="project-dialog-head">
        <Dialog.Title className="project-dialog-title">
          <ShieldCheck size={14} aria-hidden="true" /> 持久审批规则
        </Dialog.Title>
        <Dialog.Close asChild>
          <button className="icon-btn project-dialog-close" aria-label="关闭">
            <X size={14} />
          </button>
        </Dialog.Close>
      </div>
      <Dialog.Description className="project-dialog-desc">
        项目级「以后都允许」规则。规则由审批卡创建（本面板只读展示与撤销）；
        撤销后，同项目新会话遇到同一操作会重新弹出审批。
      </Dialog.Description>

      <div className="approve-policy-toolbar">
        <span className="approve-policy-count" role="status">
          {rules === null ? '正在加载…' : `共 ${rules.length} 条规则`}
        </span>
        <button
          type="button"
          className="approve-policy-refresh"
          onClick={() => void load()}
          aria-label="刷新持久审批规则"
        >
          <RefreshCw size={14} /> 刷新
        </button>
      </div>

      {error && (
        <div className="approve-policy-error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="approve-policy-notice" role="status">
          {notice}
        </div>
      )}

      {rules !== null && rules.length === 0 && !error && (
        <div className="approve-policy-empty" role="status">
          暂无持久审批规则。在审批卡上选择「以后都允许」即可创建。
        </div>
      )}

      {rules !== null && rules.length > 0 && (
        <ul className="approve-policy-list">
          {rules.map((rule) => (
            <li key={rule.id} className="approve-policy-row">
              <div className="approve-policy-row-head">
                <span className="approve-policy-tool">{rule.tool}</span>
                <span className="approve-policy-granularity">
                  {GRANULARITY_LABELS[rule.granularity]}
                </span>
              </div>
              <code className="approve-policy-key" title={rule.key}>
                {rule.key}
              </code>
              <div className="approve-policy-meta">
                <span>权限：{rule.permission_at_approval}</span>
                <time dateTime={rule.created_at}>{rule.created_at}</time>
                <span className="approve-policy-id" title={rule.id}>
                  {rule.id.slice(0, 8)}
                </span>
              </div>
              {confirmId === rule.id ? (
                <div className="approve-policy-confirm" role="group" aria-label="确认撤销">
                  <span>撤销后此操作将重新需要审批。</span>
                  <button
                    type="button"
                    className="approve-policy-danger"
                    disabled={busyId === rule.id}
                    onClick={() => void revoke(rule.id)}
                  >
                    {busyId === rule.id ? '撤销中…' : '确认撤销'}
                  </button>
                  <button
                    type="button"
                    className="approve-policy-quiet"
                    onClick={() => setConfirmId(null)}
                  >
                    取消
                  </button>
                </div>
              ) : (
                <div className="approve-policy-actions">
                  <button
                    type="button"
                    className="approve-policy-quiet"
                    onClick={() => {
                      setNotice(null);
                      setConfirmId(rule.id);
                    }}
                  >
                    <Trash2 size={13} /> 撤销
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      <div className="project-dialog-actions">
        <Dialog.Close asChild>
          <button className="project-btn project-btn-primary">关闭</button>
        </Dialog.Close>
      </div>
    </Dialog.Content>
  );
}
