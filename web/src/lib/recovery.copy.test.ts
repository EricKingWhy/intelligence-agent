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
     ⇒ 因此本面板的文案**不得**出现"只有它才能解开 / 别的档都不行"这类独占宣称。

     ## 为什么这里**只有**逐字钉，没有"中文独占句式检测器"（四轮的教训，别再往回加）
     这条要求被追着改了四轮，形态依次是：字面子串 → 枚举式量词正则 → 语义判据
     （独占标记 **或** 「才/就/方…解开」顺序式）→ 窄假话形状表。**每一轮都被更自然的
     句子击穿，且最终形态出现了比"漏网"更坏的方向——假阳性**：被判成独占宣称的句子里，
     有**正在声明非独占**的（`它不是唯一能解开这条会话的选项。` 命中 `/唯一…解(开|锁)/`）、
     有显式否定的（`不是只有它才能解锁。`）、有把假话当反面教材引用的
     （`非它不可这种说法是错的。`）。而"漏网"侧同样没守住：`选了它这条会话就解开了`、
     `这一档就能让会话恢复`、`选它这条会话才能恢复`、`此为解开会话的必经之路` 四句
     全都不命中。
     **结论**：在任意中文里判定"是否宣称独占"不是靠正则能守住的（否定、条件、引用、
     反讽都会翻转语义），继续加形状只会同时抬高假阳性与维护成本。⇒ **整条路线放弃**，
     只保留一件确定的事：

       **逐字钉住四项 label/help**（下一用例）。任何改写都会红，逼一次人工重读；
       重读时对照上段的后端事实（除 `DEFER` 外四档都结清）判断新文案是否为真。

     这个取舍是刻意的：**"每次改动都必须有人看一眼"比"自动拦住某些句式"更可靠**，
     因为前者不会误伤诚实表述、也不会给出"已覆盖"的错觉。真正的兜底是 PRODUCT
     原则 3（真实优先）与这里的人工对账——不要因为本用例为绿就以为独占宣称已被自动拦住。

     口径说明（终轮复核 F3）：**"只有逐字钉"是本文件级的口径**——句式检测器只有本文件
     曾有；`recovery.test.ts` 的逐槽内容断言（如 `not.toContain('才会解开')`、
     `toContain('结清')`）与 `RecoveryDecisionPanel.test.tsx` 的逐槽渲染断言**不是**
     句式检测器（无假阳性面），它们不受本教训影响、继续在位。注入历史假话的消融读数
     也依赖这个事实：注入 ABANDON 槽位时本用例与 `recovery.test.ts:69` 的字面禁令
     **同中**（两文件全量 = 2 红）；注入非 ABANDON 槽位则恰 1 红（终轮复核 M3/M3b 实测）。 */
  it('选项文案逐字钉住（文案改动必须人工重读一次，含后方对账）', () => {
    // 本票文案的**唯一**守卫：把当前四档的 label/help 逐字写死。任何改写都会红。
    // 改文案时请重读上一条注释里的后端事实（除 DEFER 外四档都结清），
    // 确认新文案没有宣称"只有它才能解开/别的档都不行"，再更新本表。
    expect(DECISION_OPTIONS.map((o) => [o.verdict, o.label, o.help])).toEqual([
      [
        'CONFIRM_SUCCESS',
        '当作已生效，继续',
        '仅在你已亲眼确认结果、或查过外部系统后选择；会记下你的来源（可选）。',
      ],
      [
        'RETRY',
        '当作没生效，安全重做',
        '服务端会取消原调用，由模型安全重发起——永不盲跑原操作。',
      ],
      [
        'ABANDON',
        '当作没生效，就此打住',
        '显式取消这次调用，不再重做——它会结清这次调用；操作本身不撤销。',
      ],
      [
        'DEFER',
        '先跳过，稍后再说',
        '保持待裁决状态，稍后可以重新裁决；现在不启动任何重做。',
      ],
    ]);
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
