/** #1030（缺陷③ / AC2）：面向用户的文案不得含协议词汇。
 *
 *  后端 `RecoveryConflict` 的 message 是**写给调用方的**：
 *  `存在需要人工裁决的 UNKNOWN Operation（bash(tool_call_id=call_82ade…)）：先 POST
 *   /api/sessions/{id}/recover 携带 decisions=[{tool_call_id, verdict}] 结清
 *   （verdict ∈ CONFIRM_SUCCESS / …）`（`session/service.py:5061-5065`）。
 *  最终用户既不知道什么是 tool_call_id，也无法手写 POST——把它当用户文案，
 *  等价于对用户静默（票面现象 2）。
 *
 *  设计依据：Kubernetes Object Conditions 的 reason/message 双层（ADAPT）——
 *  默认给人话摘要，要排查的人展开仍看到原始 message，两者都不丢。
 */
import { describe, expect, it } from 'vitest';
import { adjudicationHeadline, adjudicationTechDetail, DECISION_OPTIONS } from './recovery';

/** 后端真原文（`session/service.py:5061-5065` 逐字）。 */
const PROTOCOL_MESSAGE =
  '存在需要人工裁决的 UNKNOWN Operation（bash(tool_call_id=call_82ade…)）：先 POST ' +
  '/api/sessions/{id}/recover 携带 decisions=[{tool_call_id, verdict}] 结清' +
  '（verdict ∈ CONFIRM_SUCCESS / CONFIRM_FAILURE / RETRY / ABANDON / DEFER）；' +
  '裁决前请先核查该调用的外部事实';

/** AC2 点名的协议词汇。 */
const PROTOCOL_TOKENS = ['POST /api/sessions', 'tool_call_id', 'verdict ∈', 'decisions=['];

describe('#1030 待裁决文案 · 第一层人话（AC2）', () => {
  it('条数已知时如实报数（用户能对照卡片数）', () => {
    expect(adjudicationHeadline(2)).toBe('这个会话有 2 个操作的结果不确定——需要你裁决后才能继续。');
    expect(adjudicationHeadline(1)).toBe('这个会话有 1 个操作的结果不确定——需要你裁决后才能继续。');
  });

  it('条数未知（409 只有纯文本、没带结构化清单）⇒ **不编造数字**', () => {
    // 后端给了"有待裁决"但没给条数时，写「有 1 个操作」就是凭空造数。
    const text = adjudicationHeadline(null);
    expect(text).toBe('这个会话有操作的结果不确定——需要你裁决后才能继续。');
    expect(text).not.toMatch(/\d/);
  });

  it('人话层不含任何协议词汇（AC2 的核心断言）', () => {
    for (const count of [null, 1, 2, 37]) {
      const text = adjudicationHeadline(count);
      for (const token of PROTOCOL_TOKENS) {
        expect(text, `count=${String(count)} 泄漏了 ${token}`).not.toContain(token);
      }
    }
  });

  it('人话层给出可行动的下一步（"裁决"，而不是"联系管理员"式的空话）', () => {
    expect(adjudicationHeadline(1)).toContain('裁决');
  });

  /* 审查（修复轮轴 B）P2：用户可见文案里曾有「选它这条会话才会解开」——**按字面为假**。
     后端 `ReconcileVerdict` 五值枚举里除 `DEFER` 外四者都结清账本
     （`recovery/coordinator.py:1065-1136`：CONFIRM_SUCCESS→SUCCEEDED(1071/1077)、
     CONFIRM_FAILURE→FAILED(1085/1094)、RETRY→CANCELLED(1106)、ABANDON→CANCELLED(1136)；
     区间到 1136——ABANDON 是兜底 `return`，在 1128-1136），
     而本面板给用户的四项里就有三项属于"会解开"。
     这条断言把"不得在用户可见文案里宣称独占性"这一类话钉住（PRODUCT 原则 3 真实优先）。

     补审（#1030 §8.3 #8）P3：初版正则 `/才会解开|唯一能|只有它|仅它才/` 只认这四个字面
     子串，可被最自然的同义句绕过——实测「…选它**才能**解开这条会话…」与「…且**唯有本档**
     能让这条会话继续…」都是 **0 红**。故改为量词/独占类写法，并同时扫 `label`
     （label 同样是用户可见文案，初版只扫 `help`）。 */
  const EXCLUSIVITY_CLAIM =
    /才会解(开|锁)|才能解(开|锁)|唯一|唯有|只(有|此|要)|仅(它|此|有)|其他(都|全)?不|本档才|其他三档/;
  it('选项文案不得宣称"只有它才能解开"（除 DEFER 外四档都会结清账本）', () => {
    for (const option of DECISION_OPTIONS) {
      for (const text of [option.label, option.help]) {
        expect(text, `${option.verdict} 的文案宣称了独占性：${text}`)
          .not.toMatch(EXCLUSIVITY_CLAIM);
      }
    }
  });
});

describe('#1030 待裁决文案 · 第二层技术详情（AC2：只在折叠层出现）', () => {
  it('后端原文**逐字保留**：翻译掉就等于把排查依据藏起来', () => {
    // AC2 要求"技术详情只在折叠层出现"，不是"消失"——tool_call_id 是手工结清的
    // 唯一依据，逐字保留（含端点与 verdict 枚举）。
    expect(adjudicationTechDetail(PROTOCOL_MESSAGE)).toBe(PROTOCOL_MESSAGE);
    for (const token of PROTOCOL_TOKENS) {
      expect(adjudicationTechDetail(PROTOCOL_MESSAGE)).toContain(token);
    }
  });

  it('空/缺省 ⇒ null（没有可展开的内容就不渲染折叠层，不画一个空壳）', () => {
    expect(adjudicationTechDetail(null)).toBeNull();
    expect(adjudicationTechDetail('')).toBeNull();
    expect(adjudicationTechDetail('   ')).toBeNull();
  });

  it('去首尾空白后再判断（全空白等同没有）', () => {
    expect(adjudicationTechDetail('  需人工裁决  ')).toBe('需人工裁决');
  });
});
