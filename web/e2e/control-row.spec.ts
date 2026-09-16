/** F1（Phase 2b）Composer control row e2e 交互测试。
 *
 * 验收项：
 *   - 三个档位控件 trigger 在场（权限 / Agent Profile / Reasoning Effort，同一个 `OptionPicker`）
 *   - 目录缺席（端点返空）→ 该控件不渲染（不伪造列表）
 *   - 键盘打开浮层 + 方向键导航 + Enter 选档 → trigger 文本更新
 *   - Esc 关闭浮层（§19）
 *
 * #201 之后：三个档位下拉合并为一个共享组件 `OptionPicker`（`ControlPicker` 与多选
 * `ContextProviderPicker` 均已删除）。**`aria-label` 维持原值不变**——「权限模式」是中文，
 * 另外两个继续是 `Agent Profile` / `Reasoning Effort`：票面冻结结论 B 明确「会影响到 e2e
 * 定位器就不统一中文」，所以下面按各自原值定位。（曾试图顺手统一成中文，两轴 code-review
 * 的 Spec 轴按票面结论判为 P1，已回退。）
 *
 * 车道归属：Playwright e2e（同 model-picker.spec.ts 约定）。 */

import { expect, test } from '@playwright/test';
import {
  AGENT_PROFILES,
  PERMISSION_MODES,
  REASONING_EFFORTS,
  fulfillSse,
  longCatalog,
  pickControl,
  routeApi,
} from './fixtures';

test('Composer control row：三档位控件渲染 + 键盘选档 + Esc 关闭', async ({ page }) => {
  const frames = [
    { type: 'session/started', seq: 1, session_id: 'e2e-session-0001', run_id: 'e2e-run-0001', time: '2026-09-08T00:00:00Z' },
    { type: 'run/started', seq: 2, session_id: 'e2e-session-0001', run_id: 'e2e-run-0001', time: '2026-09-08T00:00:00Z' },
    { type: 'user/message', data: { content: '测试 control row' }, seq: 3, session_id: 'e2e-session-0001', run_id: 'e2e-run-0001', step_id: 1, time: '2026-09-08T00:00:00Z' },
    { type: 'run/completed', data: {}, seq: 4, session_id: 'e2e-session-0001', run_id: 'e2e-run-0001', time: '2026-09-08T00:00:00Z' },
  ];

  await routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
    onSessionPost: (route) => fulfillSse(route, frames),
  });

  await page.goto('/');

  // 三个档位控件 trigger 在场（同一个 OptionPicker，各由调用方传 aria-label）
  const modelTrigger = page.locator('.composer-model[aria-label="模型选择"]');
  const permTrigger = page.locator('.composer-control[aria-label="权限模式"]');
  const agentTrigger = page.locator('.composer-control[aria-label="Agent Profile"]');
  const effortTrigger = page.locator('.composer-control[aria-label="Reasoning Effort"]');

  // ModelPicker 目录空时不渲染——这里没 mock models，所以 model-picker 不在场
  await expect(modelTrigger).toHaveCount(0);
  // 三个档位控件在场
  await expect(permTrigger).toBeVisible();
  await expect(agentTrigger).toBeVisible();
  await expect(effortTrigger).toBeVisible();

  // 键盘打开「权限模式」浮层
  await permTrigger.focus();
  await page.keyboard.press('Enter');
  // 浮层已开——listbox 恒可见（combobox 在短目录下会随搜索框隐藏，见 F-DEFER-1）
  await expect(page.locator('[role="listbox"]')).toBeVisible();
  // option 角色在场——默认（未选）+ auto/ask/deny = 4（FE-R11-05 加的回到未选入口）
  await expect(page.locator('[role="option"]')).toHaveCount(4);

  // Esc 关闭浮层（§19）
  await page.keyboard.press('Escape');
  await expect(page.locator('[role="listbox"]')).toBeHidden();

  // 再次打开 + Enter 选第一个真实档位（ArrowDown 1 次越过「默认（未选）」）
  await pickControl(page, '权限模式', 1, '只读');
});

test('Composer control row：长目录搜索过滤 + 短目录隐藏搜索框', async ({ page }) => {
  // 权限模式 3 条 ≤ 5 → 搜索框隐藏；要测搜索需换长目录（F-DEFER-1）。
  // 长目录来自 fixtures 公共构造，避免内联后与其它 spec 漂移。
  const LONG_MODES = longCatalog('mode', 6, { index: 2, label: '工作区写入' });

  await routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: LONG_MODES,
  });

  await page.goto('/');

  const permTrigger = page.locator('.composer-control[aria-label="权限模式"]');
  await expect(permTrigger).toBeVisible();
  await permTrigger.focus();
  await page.keyboard.press('Enter');

  // 长目录（6 > 5）→ 搜索框可见、可交互
  const combo = page.getByRole('combobox', { name: '权限模式' });
  await expect(combo).toBeVisible();
  // 6 条目录 + 「默认（未选）」= 7
  await expect(page.locator('[role="option"]')).toHaveCount(7);

  // 搜索过滤：键入「工作区」只剩匹配项（「默认（未选）」的 keywords 不含它；
  // 其余 mode N 行也不含）。夹具与断言同步改成真实文案后，原来那句 `ask`
  // 已无匹配项——这正是"断言跟着数据走"该有的样子，不是把断言放宽。
  await page.keyboard.type('工作区');
  await expect(page.locator('[role="option"]')).toHaveCount(1);
  await expect(page.locator('[role="option"]')).toContainText('工作区写入');
});

/** FE-R11-05 回归锁：单选控件选了之后必须能回到「未选」。
 *  此前只能整页 reload——目录里没有任何表达"没选"的条目，触发文本却显示 placeholder。 */
test('Composer control row：单选档位可以选回「默认（未选）」', async ({ page }) => {
  await routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
  });
  await page.goto('/');

  const trigger = page.locator('.composer-control[aria-label="权限模式"]');
  await expect(trigger).toContainText('权限'); // 未选 → placeholder（文案是「权限」）

  // 先选一个真实档位
  await pickControl(page, '权限模式', 1, '只读');
  await expect(trigger).toContainText('只读');

  // 再选回「默认（未选）」——首项，Enter 零次下压即命中
  await pickControl(page, '权限模式', 0, '权限');
  await expect(trigger).not.toContainText('只读');
});

/** #201 验收「选中态 = 勾选 + 加重 + 左侧 2px 高亮条」的**可失败**锁。
 *
 *  为什么必须在 e2e：弹层是 Radix portal（SSR 里没有），组件单测断不到；而这三条通道
 *  只活在 CSS 里——两轴 review 的 Spec 轴指出该 AC 当时**没有任何会红的测试**。
 *  三条一起断：`data-state="checked"`（第二通道的挂钩）、`.picker-item-check` 图标、
 *  `::before` 实测宽度 = 2px（第三通道；只断属性不断像素的话，把 accent 条删掉照样绿）。 */
test('Composer control row：选中行的三通道选中态（勾选 + 加重 + 2px 高亮条）', async ({ page }) => {
  await routeApi(page, { sessions: [], events: [], permissionModes: PERMISSION_MODES });
  await page.goto('/');

  const trigger = page.locator('.composer-control[aria-label="权限模式"]');
  await trigger.click();
  // 下压一次到第一个真实档位（只读），Enter 选中
  const listbox = page.locator('[role="listbox"]:visible').last();
  await expect(listbox).toBeVisible();
  await listbox.focus();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(trigger).toContainText('只读');

  // 重新打开：选中行的三通道同时在场，且只有它一行是 checked
  await trigger.click();
  const checked = page.locator('.picker-item[data-state="checked"]');
  await expect(checked).toHaveCount(1);
  await expect(checked).toContainText('只读');
  await expect(checked.locator('.picker-item-check')).toHaveCount(1);
  const barWidth = await checked.evaluate(
    (el) => getComputedStyle(el, '::before').width,
  );
  expect(barWidth, '左侧高亮条应为 2px').toBe('2px');
  // 「默认（未选）」此时是未选中态：同一行**不放勾**（否则"选中"就没有视觉差异）。
  // 用 hasText 收敛到那一行——未选态本来就有多行（ask / deny 同样 unchecked）。
  const defaultRow = page.locator('.picker-item[data-state="unchecked"]', { hasText: '默认（未选）' });
  await expect(defaultRow).toHaveCount(1);
  await expect(defaultRow.locator('.picker-item-check')).toHaveCount(0);
});

/** FE-R11-04 回归锁：短目录（搜索框 display:none）下**纯键盘**必须能选档。
 *
 *  此前的洞：cmdk 把方向键/Enter 的处理挂在 `[cmdk-root]` 上，只能靠冒泡到达；
 *  搜索框一藏，root 里没有可聚焦元素 → Radix 把焦点放到 Content（在 root 之外）
 *  → 方向键无反应、Enter 不提交。鼠标路径正常，所以只有键盘用户会撞上。
 *  本用例**不手动 focus listbox**（那是旧 helper 的绕行），只依赖打开时的初焦——
 *  修复前这里必然失败。 */
test('Composer control row：短目录键盘导航（不手动聚焦 listbox）', async ({ page }) => {
  await routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: PERMISSION_MODES, // 3 条 ≤ 5 → 搜索框隐藏
  });
  await page.goto('/');

  const trigger = page.locator('.composer-control[aria-label="权限模式"]');
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[role="listbox"]')).toBeVisible();

  // 焦点必须在 cmdk 的 root 内，否则按键根本到不了承接者
  const focusInCmdkRoot = await page.evaluate(() => {
    const a = document.activeElement as HTMLElement | null;
    return !!a?.closest('[cmdk-root]');
  });
  expect(focusInCmdkRoot).toBe(true);

  // 零下压 = 首项「默认（未选）」；下压一次 → 第一个真实档位
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(trigger).toContainText('只读');
});

test('Composer control row：提交 payload 字段名对齐后端契约', async ({ page }) => {
  const frames = [
    { type: 'session/started', seq: 1, session_id: 'e2e-session-payload', run_id: 'e2e-run-payload', time: '2026-09-08T00:00:00Z' },
    { type: 'run/started', seq: 2, session_id: 'e2e-session-payload', run_id: 'e2e-run-payload', time: '2026-09-08T00:00:00Z' },
    { type: 'user/message', data: { content: 'payload 测试' }, seq: 3, session_id: 'e2e-session-payload', run_id: 'e2e-run-payload', step_id: 1, time: '2026-09-08T00:00:00Z' },
    { type: 'run/completed', data: {}, seq: 4, session_id: 'e2e-session-payload', run_id: 'e2e-run-payload', time: '2026-09-08T00:00:00Z' },
  ];

  let capturedBody: string | null = null;

  await routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
    onSessionPost: (route) => {
      const req = route.request();
      capturedBody = req.postData() ?? '';
      return fulfillSse(route, frames);
    },
  });

  await page.goto('/');

  // 选 权限模式 → auto / Agent 档位 → coding / 推理深度 → deep
  // （每个控件首项都是「默认（未选）」，故下压次数 = 条目下标 + 1）
  await pickControl(page, '权限模式', 1, '只读');
  await pickControl(page, 'Agent Profile', 2, 'Coding');
  await pickControl(page, 'Reasoning Effort', 3, 'Deep');

  // 提交任务
  await page.getByLabel('Agent 任务').fill('payload 测试');
  await page.getByLabel('发送').click();

  // 等待 POST 被拦截
  await expect.poll(() => capturedBody).not.toBeNull();

  const body = JSON.parse(capturedBody!);
  // 字段名对齐后端 B1 契约
  expect(body.permission_mode).toBe('read-only');
  expect(body.agent_profile).toBe('coding');
  expect(body.reasoning_effort).toBe('deep');
  // #201：多选 context provider 控件已删除，UI 上没有任何入口能设这个键 →
  // 断言它不出现在 payload（后端仍接受程序化显式传值，见 web/src/lib/amend.ts）。
  expect(body.context_providers).toBeUndefined();
});

test('#201 档位收窄提示：只在真的被收窄时出现，且逐字给出 N/M', async ({ page }) => {
  /* 用户裁定（设计稿 §4）：「要提示，但从简，不能突兀」——位置固定在档位弹层的
     footer（`.picker-foot`），一行次级文字，被收窄掉的名字放 `title`。
     数字来自后端 `/api/agent-profiles` 的 `tool_scope`（纯函数
     `lib/agentProfileScope.ts` 组装，单测在 `agentProfileScope.test.ts`）；
     这条 e2e 锁的是**真实 DOM 接线**——纯函数对但没挂上去，用户一样看不到。 */
  await routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: PERMISSION_MODES,
    agentProfiles: AGENT_PROFILES,
    reasoningEfforts: REASONING_EFFORTS,
  });
  await page.goto('/');

  const trigger = page.locator('.composer-control[aria-label="Agent Profile"]');
  const foot = page.locator('.picker-foot');
  const openPicker = async () => {
    await trigger.focus();
    await page.keyboard.press('Enter');
    await expect(page.locator('[role="listbox"]:visible').last()).toBeVisible();
  };
  const closePicker = async () => {
    await page.keyboard.press('Escape');
    // 退出动画期间 listbox 仍在 DOM（与 pickControl 的注释同一成因）
    await expect(page.locator('[role="listbox"]')).toHaveCount(0);
  };

  // ① 未选档位 = 用后端默认值，运行时落到 main（assembly.py:401）——main 没被收窄
  //    ⇒ **不许**出现那一行：「共 17 个中开放 17 个」只是噪音。
  await openPicker();
  await expect(foot).toHaveCount(0);
  await closePicker();

  // ② 被收窄的档位（编程 = coding，12/17）⇒ 逐字给出那句话（**声明**口径）
  await pickControl(page, 'Agent Profile', 2, 'Coding');
  await openPicker();
  await expect(foot).toBeVisible();
  await expect(foot).toHaveText('该档位声明开放 12 个工具（全部档位声明 17 个）');
  // hover 提示只列名字、不解释原因（设计稿：「只说事实，不解释原因」）。
  // `title` 挂在文案 span 上（`.picker-foot` 是容器槽位——调用方可能放别的东西，
  // 那个槽位本身不该被强行赋予一个 title 语义）。
  const note = foot.locator('span[title]');
  await expect(note).toHaveAttribute('title', /^Coding未声明开放：/);
  await expect(note).toHaveAttribute('title', /delegate/);
  await expect(note).toHaveAttribute('title', /web_search/);
  await closePicker();

  // ③ 换回未被收窄的档位（通用 = main）⇒ 提示消失（跟着当前生效档位走，不是一次性的）
  await pickControl(page, 'Agent Profile', 1, 'Main');
  await openPicker();
  await expect(foot).toHaveCount(0);
  await closePicker();
});

test('#201 冻结行：每行 20px 图标槽恒在，未知名/缺键留空槽（不编字形）', async ({ page }) => {
  /* 行结构 = 图标槽（20px 固定宽）+ 标题 + 描述 + 选中标记（设计稿 §4）。
     本用例只锁**槽**：① 带已知 icon 名的行槽里有字形；② 后端扩展出来的档位
     （夹具 `mode-0…mode-5`，没有 `icon` 键）槽**留空但仍在**、宽度不变 ⇒ 标题
     左边界不随图标有无跳动。字形从哪来由下一条用例锁。 */
  const LONG = longCatalog('mode', 6); // 无 `icon` 键的目录（真要能被选到，故走真实载荷）
  await routeApi(page, {
    sessions: [],
    events: [],
    permissionModes: LONG,
    agentProfiles: AGENT_PROFILES, // 真实 id + 真实 icon 名：main / coding / research_review
  });
  await page.goto('/');

  // ① 带 icon 名的条目：槽里有 svg，且槽宽恒 20
  await pickControl(page, 'Agent Profile', 2, 'Coding');
  const trigger = page.locator('.composer-control[aria-label="Agent Profile"]');
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[role="listbox"]:visible').last()).toBeVisible();

  const slot = page.locator('.picker-item .picker-item-icon');
  await expect(slot.first()).toBeVisible();
  const box = await slot.first().boundingBox();
  expect(Math.round(box!.width)).toBe(20); // 固定宽 = 多行对齐的前提
  // 「默认（未选）」那一行没有图标（它不是目录条目）⇒ 空槽；真实档位行有 svg
  await expect(slot.first().locator('svg')).toHaveCount(0);
  await expect(slot.nth(1).locator('svg')).toHaveCount(1);
  await page.keyboard.press('Escape');
  await expect(page.locator('[role="listbox"]')).toHaveCount(0);

  // ② 无 `icon` 键的条目：槽仍在（宽度一样）、里面是空的
  await pickControl(page, '权限模式', 1, 'mode 0');
  const permTrigger = page.locator('.composer-control[aria-label="权限模式"]');
  await permTrigger.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[role="listbox"]:visible').last()).toBeVisible();
  const unknownRow = page.locator('.picker-item', { hasText: 'mode 1' }).first();
  const unknownSlot = unknownRow.locator('.picker-item-icon');
  await expect(unknownSlot).toBeAttached();
  await expect(unknownSlot.locator('svg')).toHaveCount(0); // 空槽，不是编出来的字形
  const unknownBox = await unknownSlot.boundingBox();
  expect(Math.round(unknownBox!.width)).toBe(20);
});

test('#214：行首字形由条目声明的 icon 名决定（未知名 / 缺键留空槽，不拿 id 猜）', async ({ page }) => {
  /* #214 的判据全在这条里——它要能区分三种条目，而不是"看起来有图标"：
       ① `icon: 'layers'`（后端内置名）→ 槽里有 svg；
       ② `icon: 'sparkles'`（前端还不认识的名）→ 空槽（后端可以先行加名，前端不报错、
          也不编字形）；
       ③ **同一个 id**（`read-only`，真实 PermissionPolicy 值）但载荷**缺 `icon` 键**
          （老载荷 / 部署自定义条目）→ 空槽。
     ③ 是"前端不再按 id 猜"的判定性证据：id 一样，字形有无只看键在不在。 */
  const PROFILES = [
    ...AGENT_PROFILES,
    { id: 'deploy-custom', display_name: '部署档位', description: '带前端不认识的名', icon: 'sparkles' },
  ];
  await routeApi(page, {
    sessions: [],
    events: [],
    // 同一个 id 只换个载荷：真实后端会带 icon（见本文件上一条用例用的是同一份 id 的真载荷）
    permissionModes: [{ id: 'read-only', display_name: '只读（缺 icon）', description: '老载荷 / 部署自定义' }],
    agentProfiles: PROFILES,
  });
  await page.goto('/');

  // ①②：同一个 picker 里两种条目并排，槽的差异只来自声明的名
  await pickControl(page, 'Agent Profile', 1, 'Main');
  const trigger = page.locator('.composer-control[aria-label="Agent Profile"]');
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[role="listbox"]:visible').last()).toBeVisible();
  const row = (title: string) => page.locator('.picker-item', { hasText: title }).first();
  await expect(row('Main').locator('.picker-item-icon svg')).toHaveCount(1); // ① layers
  await expect(row('部署档位').locator('.picker-item-icon svg')).toHaveCount(0); // ② sparkles
  await page.keyboard.press('Escape');
  await expect(page.locator('[role="listbox"]')).toHaveCount(0);

  // ③ 同一 id（read-only）缺 `icon` 键 → 空槽
  await pickControl(page, '权限模式', 1, '只读（缺 icon）');
  const permTrigger = page.locator('.composer-control[aria-label="权限模式"]');
  await permTrigger.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('[role="listbox"]:visible').last()).toBeVisible();
  const noKeyRow = page.locator('.picker-item', { hasText: '只读（缺 icon）' }).first();
  await expect(noKeyRow.locator('.picker-item-icon')).toBeAttached();
  await expect(noKeyRow.locator('.picker-item-icon svg')).toHaveCount(0);
  // 槽宽不变 ⇒ 有没有图标都不影响这一行的左对齐
  const noKeyBox = await noKeyRow.locator('.picker-item-icon').boundingBox();
  expect(Math.round(noKeyBox!.width)).toBe(20);
});
