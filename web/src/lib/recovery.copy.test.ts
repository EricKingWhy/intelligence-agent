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

     ## 为什么最终**不是**用"中文独占句式检测器"来守这件事（三轮的教训）
     这条要求被三轮追着改，形态依次是：字面子串 → 枚举式量词正则 → 语义判据
     （独占标记 **或** 「才/就/方…解开」顺序式）。第三版被独立复核实测出 **4/20 假阳性**，
     且最坏的一条恰恰是**在声明非独占**的句子被判成独占宣称：
       `它不是唯一的选择：只是不再重做，也不会撤销已经发生的副作用。` → 命中 `唯一`
       `只有在后端返回 NEED_RECONCILE 时才会出现这个面板。`               → 命中 `只有`（条件句）
       `会话能继续往下走——任何一档（除跳过外）都能让它继续。`             → 命中 `能…继续`
       `点了它就会结清这次调用，仅此而已，不代表结果一定成功。`             → 命中 `仅此`
     根因：`唯一`/`只有`/`仅此` 不区分**否定**与**条件**语境，`能…继续` 无视"任何一档都能"。
     ⇒ **假阳性比漏网更坏**：一个惩罚诚实表述的守卫会把作者逼向含糊话，与 PRODUCT 原则 3 相反。
     所以本文件改用**两件确定性的事**（都不依赖对任意中文的句式判断）：
       ① **逐字钉住当前文案**（下一用例）——文案一变就红，强制人工重读一次。这是本仓既有的
          文案约定形态（`expect(...).toBe('<整句>')` 在 `web/src/lib/**` 有上百处先例）；
       ② **一个窄的、显式排除否定/条件的假话表**（`SETTLES_ALONE`）——它只覆盖"别的档都不行/
          只有它才能解开"这类**我们真见过**的假话形状，命中即红；并有反向样例证明它不误伤诚实表述。
     **能力边界（明写，勿读成已完备）**：②不是"中文独占句式"的完备检测器，仍是形状枚举；
     拿任意一句没见过的同义句都可能漏。真正的兜底是 PRODUCT 原则 3 与四项 `help` 的**人工逐条
     对账**：后端除 `DEFER` 外四档都结清，任何"只有它才能解开"式的说法都是假话。 */
  it('选项文案逐字钉住（文案改动必须人工重读一次，含后方对账）', () => {
    // 这是本票文案的真正守卫：把当前四档的 label/help 逐字写死。任何改写都会红。
    // 改文案时请同时重读上一条注释里的后端事实（除 DEFER 外四档都结清）再更新本表。
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

  /** 「宣称别的档都不行 / 只有它行」的**已知**假话形状。窄、显式、不追求完备。 */
  const SETTLES_ALONE = [
    /才会解(开|锁)/,
    /才能解(开|锁)/,
    /方能解(开|锁)/,
    /唯一.{0,6}(能|可以|会)?解(开|锁)/,
    /唯有.{0,8}(能|可以|会)/,
    /唯有本档/,
    /只有它/,
    /仅(它|此档|此一档)才/,
    /非它不可/,
    /其他(三档|都|全)?(都|全)?不(行|可以|能)/,
    /别的档都不行/,
  ];
  it('选项文案不得出现"只有它才能解开"这类已知假话（含反向样例：不得误伤诚实表述）', () => {
    // ① 真话不得命中：四项产品文案（当前文本）逐条过。
    for (const option of DECISION_OPTIONS) {
      for (const text of [option.label, option.help]) {
        const hit = SETTLES_ALONE.find((re) => re.test(text));
        expect(hit, `${option.verdict} 的文案命中假话形状 ${String(hit)}：${text}`).toBeUndefined();
      }
    }
    // ② 已知假话必须命中（含历史原文与两轮实测漏网——本表即其回归锁）。
    const MUST_BE_CAUGHT = [
      '选它这条会话才会解开',        // 初版原文（轴 B MUT-A）
      '选它才能解开这条会话',        // 第一轮实测漏网
      '选它方能解开这条会话',        // 第二轮实测漏网
      '唯有本档能让这条会话继续',    // 第一轮实测漏网
      '只有它才能解锁',
      '唯一能解开的就是它',
      '其他三档都不行',
      '非它不可，选这个才有用',
    ];
    for (const text of MUST_BE_CAUGHT) {
      expect(SETTLES_ALONE.some((re) => re.test(text)), `假话溜过了：${text}`).toBe(true);
    }
    // ③ **反向样例（假阳性回归锁）**：这些句子是诚实的，且在第三版语义判据下**被误伤过**。
    //    尤其第一条是在**声明非独占**——被判成独占宣称会直接把守卫的语义搞反。
    const MUST_NOT_BE_CAUGHT = [
      '它不是唯一的选择：只是不再重做，也不会撤销已经发生的副作用。',
      '只有在后端返回 NEED_RECONCILE 时才会出现这个面板。',
      '会话能继续往下走——任何一档（除跳过外）都能让它继续。',
      '点了它就会结清这次调用，仅此而已，不代表结果一定成功。',
      '别的档也都能让会话继续，这里只是默认项。',
      '这一档会结清这次调用；其他档同样能解开，差别在是否重做。',
    ];
    for (const text of MUST_NOT_BE_CAUGHT) {
      const hit = SETTLES_ALONE.find((re) => re.test(text));
      expect(hit, `误伤了诚实文案（命中 ${String(hit)}）：${text}`).toBeUndefined();
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
