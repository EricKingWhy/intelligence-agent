/** #1367（P0-3）：错误文案分层（401 / 400 / 断网 / 未知）。
 *
 *  判据只看 `describeFailure` 的**输出**：一句人话原因 + 一句去哪里改 + 原始详情。
 *  输入是两种既有事实（投影的 `run_failure` 与单槽横幅原文），本文件不改上报链路。
 *  红线：面向用户的 title/hint 不许出现「见后端日志」（Lane H 卡点 2）。 */
import { describe, expect, it } from 'vitest';
import { describeFailure } from './useSession';

/** 后端未分类兜底句（`model/failure.py` 模板，HTTP 状态码在括号里）。 */
const unclassified = (status: number) =>
  `提交失败：运行失败（BadRequestError，HTTP ${status}），未分类异常；完整原始信息见后端日志`;

describe('describeFailure — 401 与 400 可区分', () => {
  it('401 → 密钥不对 + 指向 MODEL_API_KEY', () => {
    const copy = describeFailure(unclassified(401));
    expect(copy.kind).toBe('auth');
    expect(copy.title).toContain('密钥');
    expect(copy.hint).toContain('MODEL_API_KEY');
    expect(copy.detail).toContain('HTTP 401'); // 原始事实逐字保留
  });

  it('400 → 模型名不存在 + 指向 MODEL_NAME', () => {
    const copy = describeFailure(unclassified(400));
    expect(copy.kind).toBe('model');
    expect(copy.title).toContain('模型名不存在');
    expect(copy.hint).toContain('MODEL_NAME');
    expect(copy.hint).toContain('模型选择器');
  });

  it('同一句兜底文案下，401 与 400 的呈现层结论必须不同', () => {
    const auth = describeFailure(unclassified(401));
    const model = describeFailure(unclassified(400));
    expect(auth.title).not.toBe(model.title);
    expect(auth.title).not.toContain('模型名');
    expect(model.title).not.toContain('密钥');
  });

  it('后端分类命中的 reason 走同一条分层（不依赖 HTTP 字样）', () => {
    expect(describeFailure({ reason: 'provider_auth_failed', message: '模型供应商鉴权失败' }).kind).toBe('auth');
    expect(
      describeFailure({ reason: 'provider_model_not_found', message: '模型不存在' }).kind,
    ).toBe('model');
  });

  it('403 按鉴权处理（同一条「密钥/权限」处置路径）', () => {
    expect(describeFailure(unclassified(403)).kind).toBe('auth');
  });
});

describe('describeFailure — 断网', () => {
  it('浏览器裸 Failed to fetch → 人话 + 检查网络；可重试时文案指向重试按钮', () => {
    const copy = describeFailure('提交失败：Failed to fetch', { retryable: true });
    expect(copy.kind).toBe('network');
    expect(copy.title).toContain('连不上服务');
    expect(copy.hint).toContain('网络');
    expect(copy.hint).toContain('重试');
    expect(copy.detail).toBe('提交失败：Failed to fetch'); // 原文不丢
  });

  it('没有重试动作时不承诺「点重试」（改为重新发送）', () => {
    const copy = describeFailure('续聊失败：Failed to fetch');
    expect(copy.kind).toBe('network');
    expect(copy.retryable).toBe(false);
    expect(copy.hint).not.toContain('点重试');
    expect(copy.hint).toContain('重新发送');
  });

  it('后端「无法连接模型供应商」归为同一类断网', () => {
    const copy = describeFailure({
      reason: 'APIConnectionError',
      message: '运行失败（APIConnectionError）：无法连接模型供应商（网络或网关不可达）；完整原始信息见后端日志',
    });
    expect(copy.kind).toBe('network');
  });
});

describe('describeFailure — 未知 / 服务端错误', () => {
  it('未知兜底：人话层剥掉「见后端日志」，原文仍在折叠详情里', () => {
    const copy = describeFailure(unclassified(418));
    expect(copy.kind).toBe('unknown');
    expect(copy.title).not.toContain('后端日志');
    expect(copy.hint).toBe('');
    expect(copy.detail).toContain('完整原始信息见后端日志');
  });

  it('5xx → 服务端暂时不可用', () => {
    const copy = describeFailure(unclassified(503));
    expect(copy.kind).toBe('server');
    expect(copy.title).toContain('503');
  });

  it('所有分支的 title/hint 都不出现「后端日志」', () => {
    const inputs: Array<string | { reason: string; message: string }> = [
      unclassified(401),
      unclassified(400),
      unclassified(418),
      unclassified(503),
      '提交失败：Failed to fetch',
      { reason: 'provider_content_moderation', message: 'provider 内容审查拒绝输入（可能因检索到的网页文本）' },
    ];
    for (const input of inputs) {
      const copy = describeFailure(input);
      expect(copy.title).not.toContain('后端日志');
      expect(copy.hint).not.toContain('后端日志');
    }
  });
});
