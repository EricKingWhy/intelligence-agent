/** #1367（P0-3）：对话区内联错误条（`ErrorBar`）的渲染契约。
 *
 *  锁四件事：
 *   1. run 失败的内联条出现在**对话区**（不再只活在右侧 Inspector）；
 *   2. 复用既有 alert token（`approval-error`）而不是第三套样式；
 *   3. 原始详情默认折叠（一行高），原文逐字保留在 `<details>` 里；
 *   4. 断网这一类带可见「重试」，run 失败不带（重发语义不同）。
 */
import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import type { AgentEvent, ConversationState } from '../types';
import { initConversation, projectHistory } from '../lib/projection';
import { describeFailure } from '../hooks/useSession';
import { Conversation, ErrorBar } from './Conversation';

const SID = 's-1367';

function failedRun(over: Record<string, unknown> = {}): ConversationState {
  const events = [
    { type: 'run/started', data: {}, seq: 1, session_id: SID, run_id: 'r-1' },
    { type: 'user/message', data: { content: '跑一个任务' }, seq: 2, session_id: SID, run_id: 'r-1', step_id: 1 },
    {
      type: 'run/failed',
      data: {
        reason: 'provider_auth_failed',
        message: '模型供应商鉴权失败（API Key 无效或无权限），请检查供应商凭证配置',
        ...over,
      },
      seq: 3,
      session_id: SID,
      run_id: 'r-1',
    },
  ] as AgentEvent[];
  return projectHistory(SID, events);
}

function renderConversation(props: Record<string, unknown>): string {
  return renderToStaticMarkup(
    <Conversation
      conversation={failedRun()}
      loadingHistory={false}
      density="balanced"
      {...props}
    />,
  );
}

describe('ErrorBar — 视觉与折叠契约', () => {
  it('复用既有 alert token（approval-error）+ 内联布局类', () => {
    const html = renderToStaticMarkup(
      <ErrorBar copy={describeFailure('提交失败：Failed to fetch', { retryable: true })} onRetry={() => {}} />,
    );
    // 解析出类名集合再断言（审查 S-P2-4）：不钉死顺序，且能证明两个类都在同一个
    // 元素上（`approval-error` 是复用的 alert token，`conversation-error` 是布局类）。
    const classes = (/class="([^"]*approval-error[^"]*)"/.exec(html)?.[1] ?? '').split(/\s+/);
    expect(classes).toEqual(expect.arrayContaining(['approval-error', 'conversation-error']));
    expect(html).toContain('role="alert"');
    expect(html).toContain('conversation-error-icon');
  });

  it('原始详情默认折叠：<details> 无 open，收起时只有标题/提示/摘要', () => {
    const html = renderToStaticMarkup(
      <ErrorBar copy={describeFailure('提交失败：Failed to fetch', { retryable: true })} />,
    );
    expect(html).toContain('<details class="conversation-error-detail">');
    expect(html).not.toMatch(/<details[^>]*\sopen/);
    expect(html).toContain('<summary>原始详情</summary>');
    expect(html).toContain('Failed to fetch'); // 原文不丢
  });

  it('没有重试动作时不渲染按钮；有则渲染', () => {
    const copy = describeFailure('提交失败：Failed to fetch', { retryable: true });
    expect(renderToStaticMarkup(<ErrorBar copy={copy} />)).not.toContain('conversation-error-retry');
    const idle = renderToStaticMarkup(<ErrorBar copy={copy} onRetry={() => {}} />);
    expect(idle).toContain('conversation-error-retry');
    expect(idle).toContain('>重试<');
  });

  it('重试在途时按钮禁用（连点不再发第二个请求，审查 P1-2）', () => {
    const copy = describeFailure('提交失败：Failed to fetch', { retryable: true });
    const html = renderToStaticMarkup(<ErrorBar copy={copy} onRetry={() => {}} retryPending />);
    expect(html).toMatch(/<button[^>]*conversation-error-retry[^>]*disabled/);
  });
});

describe('Conversation — run 失败的内联条', () => {
  it('failed run：对话区内联一条错误条，含人话原因 + 去哪里改', () => {
    const html = renderConversation({});
    expect(html).toContain('conversation-error');
    expect(html).toContain('模型的密钥不对');
    expect(html).toContain('MODEL_API_KEY');
  });

  it('取消的 run 不算错误：不渲染内联条', () => {
    const events = [
      { type: 'run/started', data: {}, seq: 1, session_id: SID, run_id: 'r-2' },
      { type: 'user/message', data: { content: '任务' }, seq: 2, session_id: SID, run_id: 'r-2', step_id: 1 },
      { type: 'run/failed', data: { reason: 'cancelled' }, seq: 3, session_id: SID, run_id: 'r-2' },
    ] as AgentEvent[];
    const html = renderToStaticMarkup(
      <Conversation conversation={projectHistory(SID, events)} loadingHistory={false} density="balanced" />,
    );
    expect(html).not.toContain('conversation-error');
  });

  it('suppressRunFailure：让位给已有的 plan-stuck 横幅（同一事实不重复报）', () => {
    expect(renderConversation({ suppressRunFailure: true })).not.toContain('conversation-error');
  });

  it('空对话里也渲染 run 失败条（turn 还没投影出来时同样不能静默）', () => {
    const html = renderToStaticMarkup(
      <Conversation
        conversation={{ ...initConversation(SID), run_status: 'failed', run_failure: { reason: 'provider_model_not_found', message: '模型不存在' } }}
        loadingHistory={false}
        density="balanced"
      />,
    );
    expect(html).toContain('conversation-error');
    expect(html).toContain('这个模型名不存在');
  });
});

describe('Conversation — 断网提交失败的内联条', () => {
  it('带可见「重试」；两种失败各呈现一次（submitFailure 不再遮掉 run 失败说明）', () => {
    const html = renderToStaticMarkup(
      <Conversation
        conversation={failedRun()}
        loadingHistory={false}
        density="balanced"
        submitFailure={describeFailure('提交失败：Failed to fetch', { retryable: true })}
        onRetrySubmit={() => {}}
      />,
    );
    expect(html).toContain('连不上服务');
    expect(html).toContain('conversation-error-retry');
    // 审查 P3：先前 run 的 401 说明与新的发送失败都在场，不互相遮盖。
    expect(html).toContain('模型的密钥不对');
  });

  it('不可重试的断网（已被受理的迟到失败）不给按钮', () => {
    const html = renderToStaticMarkup(
      <Conversation
        conversation={initConversation(SID)}
        loadingHistory={false}
        density="balanced"
        submitFailure={describeFailure('续聊失败：Failed to fetch', { retryable: false })}
        onRetrySubmit={() => {}}
      />,
    );
    expect(html).toContain('连不上服务');
    expect(html).not.toContain('conversation-error-retry');
  });
});
