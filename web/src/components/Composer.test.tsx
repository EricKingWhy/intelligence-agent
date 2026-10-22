/** Composer 模型选择器（T10 #103 → Phase 2a Radix Popover）——SSR 渲染契约。
 *  列表来自端点（零硬编码模型名）；端点缺席（models 空）→ 选择器降级隐藏。
 *  选择器现在由 ModelPicker（Radix DropdownMenu）渲染：
 *   - trigger 是 button.composer-model，显示当前选中 label；
 *   - 选中 null 显示「默认链」，选中模型显示模型名；
 *  DropdownMenu 的弹出内容（具体模型条目）由 Radix portal 客户端渲染，不出现在 SSR。 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import type { ModelCatalogEntry } from '../lib/api';
import { Composer } from './Composer';

const noop = () => {};
// #1015：懒建入口是 Composer 的必填 prop；这些 SSR 契约测试不涉及附图，给空实现。
const base = { streaming: false, onSubmit: noop, onCancel: noop, onEnsureSession: async () => null };

const CATALOG: ModelCatalogEntry[] = [
  { name: 'deepseek-v4-flash-0731', provider: 'senseaudio', model: 'deepseek-v4-flash-0731', default: true },
  { name: 'qwen-max', provider: 'senseaudio', model: 'qwen3.8-max-0902', default: false },
];

// #1370 P0-9：权限 chip 的原生 title = 说明段 + 禁用原因段（两段并排保留）。
// ADR-0041 D4：说明段必须写「下一轮 run 生效」，不得承诺「立即」。
const PERMISSION_HINT = '权限：工具调用如何批准；改档从下一轮 run 起生效（本轮不受影响）。';

describe('Composer 模型选择器（#103 / Phase 2a）', () => {
  it('端点缺席（models 空）：选择器不渲染——不伪造列表', () => {
    const html = renderToString(createElement(Composer, { ...base, models: [], selectedModel: null, onModelChange: noop }))
      .replaceAll('<!-- -->', '');
    expect(html).not.toContain('composer-model');
    expect(html).not.toContain('model-picker');
    expect(html).toContain('composer'); // 输入框照常在场
  });

  it('目录在场：trigger 渲染 + 显示「默认链」当前选中（null 选中）', () => {
    const html = renderToString(createElement(Composer, { ...base, models: CATALOG, selectedModel: null, onModelChange: noop }))
      .replaceAll('<!-- -->', '');
    expect(html).toContain('composer-model');
    expect(html).toContain('默认链');
    expect(html).toContain('aria-label="模型选择"');
  });

  it('selectedModel 有值 → trigger 显示该模型名', () => {
    const html = renderToString(
      createElement(Composer, { ...base, models: CATALOG, selectedModel: 'qwen-max', onModelChange: noop }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('composer-model');
    expect(html).toContain('qwen-max');
    // 不应再显示默认链 label（因为切到了具体模型）
    expect(html).not.toContain('>默认链<');
  });
});

describe('Composer 输入提示按态切换（#1370 P0-7）', () => {
  it('空闲态：只说 Enter 发送，不出现排队/打断（没有排队对象）', () => {
    const html = renderToString(createElement(Composer, { ...base, streaming: false }))
      .replaceAll('<!-- -->', '');
    expect(html).toContain('placeholder="描述一个任务…（Enter 发送）"');
    expect(html).not.toContain('排队');
    expect(html).not.toContain('打断');
  });

  it('有 run 在跑：才区分 Enter 排队 / 打断，不再出现「立即」', () => {
    const html = renderToString(createElement(Composer, { ...base, streaming: true }))
      .replaceAll('<!-- -->', '');
    expect(html).toContain('Enter 排队');
    expect(html).toContain('+Enter 打断');
    expect(html).not.toContain('立即');
  });
});

describe('Composer 队列条（ADR-0030 §5.2）', () => {
  const queue = [
    { kind: 'queue' as const, id: 'q-1', content: '排队的问题', seq: 1, created_at: '' },
  ];

  it('空队列不渲染队列条（不占位不闪烁）', () => {
    const html = renderToString(createElement(Composer, { ...base, undelivered: [] }))
      .replaceAll('<!-- -->', '');
    expect(html).not.toContain('queue-bar');
  });

  it('有排队项 → 渲染条目 + 三个中文 aria 动作（编辑/立即/取消）', () => {
    const html = renderToString(
      createElement(Composer, {
        ...base, undelivered: queue,
        onEditItem: noop, onSteerItem: noop, onCancelItem: noop,
      }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('queue-bar');
    expect(html).toContain('排队的问题');
    expect(html).toContain('aria-label="编辑排队消息"');
    expect(html).toContain('aria-label="立即发送"');
    expect(html).toContain('aria-label="取消排队消息"');
  });

  it('带图的排队项渲染「附图 N 张」指示（投递前缩略图必然 404，所以只给张数）', () => {
    const html = renderToString(
      createElement(Composer, {
        ...base,
        undelivered: [
          {
            kind: 'queue' as const,
            id: 'q-2',
            content: '排队里的图',
            seq: 2,
            created_at: '',
            attachments: [
              { kind: 'image', attachment_id: 'sha256:aa', media_type: 'image/png', bytes: 1, width: 1, height: 1 },
              { kind: 'image', attachment_id: 'sha256:bb', media_type: 'image/png', bytes: 1, width: 1, height: 1 },
            ],
          },
        ],
      }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('附图 2 张');
    expect(html).toContain('queue-item-attachments');
    // 只给张数：受控读回在投递前是 404（引用还没进事件流），缩略图会是一排"加载失败"。
    expect(html).not.toContain('<img');
  });

  it('不带图的排队项不渲染附图指示（不占位）', () => {
    const html = renderToString(
      createElement(Composer, {
        ...base,
        undelivered: [{ kind: 'queue' as const, id: 'q-3', content: '纯文本', seq: 3, created_at: '' }],
      }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('纯文本');
    // 用类名判定"没渲染这条指示"：Composer 别处（附图入口的 aria/title）本来含"附图"二字。
    expect(html).not.toContain('queue-item-attachments');
  });

  it('steer 项不渲染三个动作（无后端取消/编辑通道，点了就是静默 404）', () => {
    const html = renderToString(
      createElement(Composer, {
        ...base,
        undelivered: [{ kind: 'steer' as const, id: 's-1', content: '引导的问题', seq: 2, created_at: '' }],
        onEditItem: noop, onSteerItem: noop, onCancelItem: noop,
      }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('引导的问题');
    expect(html).toContain('引导'); // 徽标仍在（状态可见）
    expect(html).not.toContain('aria-label="编辑排队消息"');
    expect(html).not.toContain('aria-label="取消排队消息"');
  });
});

describe('Composer pending constraint input lock', () => {
  it('blocks new messages and explains that the pending question must be answered first', () => {
    const html = renderToString(createElement(Composer, {
      ...base,
      constraintInputPending: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('请先确认当前要求，再发送新消息');
    expect(html).toContain('等待你确认当前要求…');
    expect(html).toMatch(/<textarea[^>]*disabled=""/);
  });
});

// ── #1223：`ask_user` 通用提问也参与 composer 锁定，文案**中性**（ask_user 不是约束澄清）──
describe('Composer pending ask_user lock (#1223)', () => {
  it('待回答的 ask_user 提问 → 锁定输入框，提示中性（绝不出现「约束澄清」字样）', () => {
    const html = renderToString(createElement(Composer, {
      ...base,
      askUserInputPending: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toMatch(/<textarea[^>]*disabled=""/);
    expect(html).toContain('请先回答当前提问，再发送新消息');
    expect(html).toContain('等待回答…');
    expect(html).not.toContain('约束澄清');
  });

  it('约束澄清在场时其锁文案**保持不动**（没有被中性的 ask_user 文案顶掉）', () => {
    const html = renderToString(createElement(Composer, {
      ...base,
      constraintInputPending: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('请先确认当前要求，再发送新消息');
    expect(html).toContain('等待你确认当前要求…');
  });
});

// ── #1356：待裁决的计划同样锁 composer（`resume_basis: 'user_input'` 的第三支）──
describe('Composer pending plan approval lock (#1356)', () => {
  it('待裁决计划 → 锁定输入框，文案说「计划裁决」而不是「审批」', () => {
    const html = renderToString(createElement(Composer, {
      ...base,
      planApprovalInputPending: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toMatch(/<textarea[^>]*disabled=""/);
    expect(html).toContain('请先决定当前计划怎么处理，再发送新消息');
    expect(html).toContain('等待你决定计划…');
    // 工具审批是另一回事：计划裁决的文案不得把用户引去找一个不存在的审批。
    expect(html).not.toContain('等待审批决策');
  });

  it('队列条与改档 pill 同样锁住，提示指向计划裁决', () => {
    const html = renderToString(createElement(Composer, {
      ...base,
      planApprovalInputPending: true,
      undelivered: [{ kind: 'queue' as const, id: 'q-1', content: '排队', seq: 1, created_at: '' }],
      onSteerItem: noop,
      onFlush: noop,
      permissionModes: [
        { id: 'read-only', display_name: '只读', description: '', icon: 'lock' },
      ],
      selectedPermissionMode: 'read-only',
      onPermissionModeChange: noop,
      permissionInSession: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('请先决定当前计划');
    expect(html).toContain(PERMISSION_HINT);
    expect(html).toContain('决定计划后再改档"');
  });
});

// ── #1223：排队条的 steer/flush 也是"开新消息"路径，与输入框同一把锁管到底 ──
describe('Composer 队列条随待回答用户输入锁定 (#1223)', () => {
  const queue = [
    { kind: 'queue' as const, id: 'q-1', content: '排队的问题', seq: 1, created_at: '' },
  ];
  // 拿出按钮开标签（disabled/aria-label/title 都在同一开标签里）。
  const buttonTag = (html: string, ariaLabel: string) =>
    html.match(new RegExp(`<button[^>]*aria-label="${ariaLabel}"[^>]*>`))?.[0] ?? '';

  it('ask_user 待回答 ⇒ 两个队列条按钮均禁用，提示中性（不写「约束澄清」）', () => {
    const html = renderToString(createElement(Composer, {
      ...base,
      undelivered: queue,
      onSteerItem: noop,
      onFlush: noop,
      askUserInputPending: true,
    })).replaceAll('<!-- -->', '');
    const steer = buttonTag(html, '立即发送');
    const flush = buttonTag(html, '立即发送全部');
    expect(steer).toContain('disabled=""');
    expect(flush).toContain('disabled=""');
    expect(steer).toContain('请先回答当前提问');
    expect(flush).toContain('请先回答当前提问');
    expect(steer).not.toContain('约束澄清');
  });

  it('约束澄清待回答 ⇒ 两个队列条按钮同样禁用（原有行为不回归）', () => {
    const html = renderToString(createElement(Composer, {
      ...base,
      undelivered: queue,
      onSteerItem: noop,
      onFlush: noop,
      constraintInputPending: true,
    })).replaceAll('<!-- -->', '');
    expect(buttonTag(html, '立即发送')).toContain('请先确认当前要求');
    expect(buttonTag(html, '立即发送全部')).toContain('disabled=""');
  });

  // #1283 P2-2：编辑→保存同样走 `POST /messages {queue_id}`，撞同一个待答 409 ⇒
  // 与 steer/flush 同锁；否则用户能打开编辑却被拒，改好的文本还会随编辑态一起消失。
  it('#1283 P2-2：ask_user 待答 ⇒ 「编辑」入口同样禁用（不给必然 409 的死胡同）', () => {
    const html = renderToString(createElement(Composer, {
      ...base,
      undelivered: queue,
      onEditItem: noop,
      onSteerItem: noop,
      onFlush: noop,
      askUserInputPending: true,
    })).replaceAll('<!-- -->', '');
    const edit = buttonTag(html, '编辑排队消息');
    expect(edit).toContain('disabled=""');
    expect(edit).toContain('请先回答当前提问');
    expect(edit).not.toContain('约束澄清');
  });

  it('#1283 P2-2：约束澄清待答 ⇒ 「编辑」入口同样禁用（原有锁定语义不回归）', () => {
    const html = renderToString(createElement(Composer, {
      ...base,
      undelivered: queue,
      onEditItem: noop,
      onSteerItem: noop,
      onFlush: noop,
      constraintInputPending: true,
    })).replaceAll('<!-- -->', '');
    expect(buttonTag(html, '编辑排队消息')).toContain('disabled=""');
  });

  it('无待回答输入 ⇒ 队列条按钮可用（锁解除，反证锁不常驻）', () => {
    const html = renderToString(createElement(Composer, {
      ...base,
      undelivered: queue,
      onSteerItem: noop,
      onFlush: noop,
    })).replaceAll('<!-- -->', '');
    expect(buttonTag(html, '立即发送')).not.toContain('disabled');
    expect(buttonTag(html, '立即发送全部')).not.toContain('disabled');
  });
});

describe('Composer procedural rule signal (#298 R5 / #1405)', () => {
  it('reusable-rule entry lives in the add menu, never as a persistent dock control', () => {
    const html = renderToString(createElement(Composer, base)).replaceAll('<!-- -->', '');
    // 附件「+」入口仍在；规则开关不再是 dock 里的常驻 checkbox（Radix portal 只在打开时渲染）。
    expect(html).toContain('aria-label="添加图片"');
    expect(html).not.toContain('将这条消息作为可复用规则');
    expect(html).not.toContain('remember-as-procedural-rule');
  });
});

// ── #283（F18-B）：会话内可改权限档 + 中性禁用文案 ──
// pill 的**触发按钮**不在 Radix portal 里，所以它的禁用态与 title 是 SSR 可断的；浮层内的
// 升档确认面（role/aria + 取消不发请求）在 `e2e/control-row.spec.ts` 里锁——本仓没有 jsdom。

describe('Composer 权限 pill（#283）', () => {
  // 逐字对齐后端 PermissionPolicy 三档（`tooling/contract.py`），同 e2e fixtures 的口径。
  const MODES = [
    { id: 'read-only', display_name: '只读', description: '可读文件和运行只读工具，不可写入。', icon: 'lock' },
    { id: 'workspace-write', display_name: '工作区写入', description: '可读写工作区内文件；高危工具仍需审批。', icon: 'pencil' },
    { id: 'danger-full-access', display_name: '完全访问', description: '所有工具无需审批，含网络与系统副作用。仅在可信环境使用。', icon: 'unlock' },
  ];

  it('会话内且不忙 → pill **可交互**（#236 的「会话内一律只读」已随本票删除）', () => {
    const html = renderToString(createElement(Composer, {
      ...base, permissionModes: MODES, selectedPermissionMode: 'read-only',
      onPermissionModeChange: noop, permissionInSession: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-label="权限模式"');
    expect(html).not.toContain('aria-disabled="true"');
    // 假提示必须消失：改档可行之后「权限档在会话创建时确定，会话内不可修改」就是假话。
    expect(html).not.toContain('权限档在会话创建时确定，会话内不可修改');
  });

  it('AC4：本轮进行中 → pill 仍禁用，且原因是**中性**的（不再宣称档位不可变）', () => {
    const html = renderToString(createElement(Composer, {
      ...base, streaming: true, permissionModes: MODES, selectedPermissionMode: 'read-only',
      onPermissionModeChange: noop, permissionInSession: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain(PERMISSION_HINT);
    expect(html).toContain('本轮进行中，等这一轮结束再改档');
    expect(html).not.toContain('权限档在会话创建时确定，会话内不可修改');
  });

  it('AC4：等待审批决策 → pill 仍禁用，且原因是**中性**的', () => {
    const html = renderToString(createElement(Composer, {
      ...base, approvalPending: true, permissionModes: MODES, selectedPermissionMode: 'read-only',
      onPermissionModeChange: noop, permissionInSession: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain(PERMISSION_HINT);
    expect(html).toContain('等待审批决策后再改档');
  });

  // #1283 P1：`locked` 已包含两种「待回答的用户输入」，改档禁用原因必须按**真实**
  // lock 原因分流——否则 ask_user 待答时用户被引去找一个并不存在的审批。
  it('#1283 P1：ask_user 待答 → 改档提示指向提问，绝不写「审批」', () => {
    const html = renderToString(createElement(Composer, {
      ...base, askUserInputPending: true, permissionModes: MODES, selectedPermissionMode: 'read-only',
      onPermissionModeChange: noop, permissionInSession: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain(PERMISSION_HINT);
    expect(html).toContain('等待回答当前提问后再改档');
    expect(html).not.toContain('审批');
  });

  it('#1283 P1：约束澄清待答 → 改档提示指向约束澄清，绝不写「审批」', () => {
    const html = renderToString(createElement(Composer, {
      ...base, constraintInputPending: true, permissionModes: MODES, selectedPermissionMode: 'read-only',
      onPermissionModeChange: noop, permissionInSession: true,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain(PERMISSION_HINT);
    expect(html).toContain('确认当前要求后再改档"');
    expect(html).not.toContain('审批');
  });

  it('新会话（permissionInSession 假）→ 仍是创建期选择，无会话内的锁', () => {
    const html = renderToString(createElement(Composer, {
      ...base, permissionModes: MODES, selectedPermissionMode: null, onPermissionModeChange: noop,
    })).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-label="权限模式"');
    expect(html).toContain('>权限<'); // 未选 → placeholder
    expect(html).not.toContain('aria-disabled="true"');
  });
});

// ── #536：预算三项收敛为单入口 popover（替换 #426 三平铺）──
// #1444：**两态（新建 / 已选会话）都渲染**。已选态的选择随下一次 `/messages`
// 提交作为 amend 发出（后端 idle→launched 消费，queued / steer 只判定丢弃）——
// 与 model 等 amend 字段同语义，不是"会话内不显示"（#308 的真实约束是在途 run
// 不改写，不是续聊提交不得携带下一 run 的预算声明）。判形/换算在映射层
// toCreateBudget / resolveDeadlineDraft（lib/amend.ts）。面板是 Radix Portal 内容，
// SSR 不可见（同权限 pill 分工）——面板内三维控件的逐项断言在 BudgetPicker.test.tsx
// （Panel 独立渲染）与 e2e/budget-entry.spec.ts（交互+wire）锁。

describe('Composer 预算入口（#536 单入口 popover）', () => {
  const budgetProps = {
    budgetRunTurns: '',
    onBudgetRunTurnsChange: noop,
    budgetRunTokens: '',
    onBudgetRunTokensChange: noop,
    budgetRunDeadline: '',
    onBudgetRunDeadlineChange: noop,
  };

  it('新会话态：单入口 trigger 在场（aria-label="预算"），全空摘要「默认」', () => {
    const html = renderToString(
      createElement(Composer, { ...base, ...budgetProps }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-label="预算"');
    expect(html).toContain('>预算：默认<');
    // 三平铺已收敛：面板外不再有常驻的预算字段行。
    expect(html).not.toContain('composer-budget-label');
  });

  it('草稿有值 → trigger 摘要随 drafts 变化（turns 5 · 500k · 2h）', () => {
    const html = renderToString(
      createElement(Composer, {
        ...base,
        budgetRunTurns: '5',
        onBudgetRunTurnsChange: noop,
        budgetRunTokens: '500000',
        onBudgetRunTokensChange: noop,
        budgetRunDeadline: '2h',
        onBudgetRunDeadlineChange: noop,
      }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('>预算：轮数 5 · 500k · 2h<');
  });

  it('会话内（permissionInSession）→ 入口同样在场，摘要「预算：默认」（#1444 两态同入口）', () => {
    const html = renderToString(
      createElement(Composer, { ...base, permissionInSession: true, ...budgetProps }),
    ).replaceAll('<!-- -->', '');
    expect(html).toContain('aria-label="预算"');
    expect(html).toContain('>预算：默认<');
  });

  it('未接任何预算 onChange → 入口不渲染；只接一维 → 入口在场（面板内只出该维）', () => {
    const none = renderToString(createElement(Composer, base)).replaceAll('<!-- -->', '');
    expect(none).not.toContain('aria-label="预算"');
    const onlyTurns = renderToString(
      createElement(Composer, {
        ...base,
        budgetRunTurns: '',
        onBudgetRunTurnsChange: noop,
      }),
    ).replaceAll('<!-- -->', '');
    expect(onlyTurns).toContain('aria-label="预算"');
  });
});
