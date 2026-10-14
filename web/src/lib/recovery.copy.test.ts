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

     ## 这条守卫的能力边界（别读成"这一类话已被整体覆盖"）
     它是**字面形状枚举 + 独占量词表**，不是完备性证明。修后实测：
       - 拦得住（各自 1 红）：`才会解开` / `才能解开` / `唯有本档` / `唯一` / `只有它` /
         `仅此` / `其他都不` / `本档才` / `其他三档`；
       - **仍可能漏**：任何未经列举的自然说法。第二轮修后复核实测 9 个同义句里仍漏 6 个
         （`选了它这条会话就解开了`、`选它方能解开`、`这一档就能让会话恢复`、
         `非它不可，会话方能继续`、`选它这条会话才能恢复`、`此为解开会话的必经之路`）
         ⇒ 已按「就/方/注定/必经之路/非它不可 + 解开|解锁|恢复|继续」补形状。
     结论性约束：**不要**因为本守卫为绿就断言"文案不会宣称独占性"；它只保证已见过的写法
     不再复现。真正的兜底是 PRODUCT 原则 3（真实优先）与四项 `help` 的人工逐条对账——
     后端除 `DEFER` 外四档都结清，任何"只有它才能解开"式的说法都是假话。 */
  /* 判据形式：**独占标记** 与 **解锁/生效类动词** 同句出现即视为宣称独占性（不看先后
     顺序——「此为解开…的必经之路」里"必经"在"解开"之后，方向枚举法会漏）。
     另含"只要选它就能恢复/继续"这类隐含独占的祈使形状。 */
  const EXCLUSIVITY_MARKER =
    /唯一|唯有|只有|仅有|仅此|唯独|其他(都|全)?不|别的(都|全)?不|非它不可|没有它不|缺它不|必经|本档才|其他三档|只此一家/;
  const EXCLUSIVITY_ORDER =
    /(才|就|方|能|可|注定)[^。；]{0,4}(解(开|锁)|恢复|继续|放行|通过)/;
  /** 判据：独占标记 **或** 「才/就/方…解开/恢复/继续」顺序式，命中即视为宣称独占性。 */
  const claimsExclusivity = (text: string): boolean =>
    EXCLUSIVITY_MARKER.test(text) || EXCLUSIVITY_ORDER.test(text);
  it('选项文案不得宣称"只有它才能解开"（除 DEFER 外四档都会结清账本）', () => {
    for (const option of DECISION_OPTIONS) {
      for (const text of [option.label, option.help]) {
        expect(claimsExclusivity(text), `${option.verdict} 的文案宣称了独占性：${text}`)
          .toBe(false);
      }
    }
  });

  it('守卫的已知形状必须真拦得住（防"正则写了但没牙"）', () => {
    // 正面样例：这些句子**按字面都为假**（除 DEFER 外四档都会结清/解开），必须被拦。
    // 其中带 ★ 的六条是第二轮修后复核实测的漏网，已按它们补形状——本用例即其回归锁。
    const MUST_BE_CAUGHT = [
      '选它这条会话才会解开',            // ★ 初版原文
      '选它才能解开这条会话',            // ★ 第一轮补的
      '唯有本档能让这条会话继续',        // ★ 第一轮补的
      '选了它这条会话就解开了',          // ★ 第二轮实测漏网
      '选它方能解开这条会话',            // ★ 第二轮实测漏网
      '这一档就能让会话恢复',            // ★ 第二轮实测漏网
      '非它不可，会话方能继续',          // ★ 第二轮实测漏网
      '选它这条会话才能恢复',            // ★ 第二轮实测漏网
      '此为解开会话的必经之路',          // ★ 第二轮实测漏网
      '只有它才能解锁',
      '唯一能解开的就是它',
      '其他三档都不行',
    ];
    for (const text of MUST_BE_CAUGHT) {
      expect(claimsExclusivity(text), `守卫漏了这个形状：${text}`).toBe(true);
    }
    // 反向对照：诚实文案不得被误伤（否则守卫会逼作者写出含糊话）。
    const MUST_PASS = [
      '显式取消这次调用，不再重做——它会结清这次调用；操作本身不撤销。',
      '服务端会取消原调用，由模型安全重发起——永不盲跑原操作。',
      '保持待裁决状态，稍后可以重新裁决；现在不启动任何重做。',
      '仅在你已亲眼确认结果、或查过外部系统后选择；会记下你的来源（可选）。',
    ];
    for (const text of MUST_PASS) {
      expect(claimsExclusivity(text), `守卫误伤了诚实文案：${text}`).toBe(false);
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
