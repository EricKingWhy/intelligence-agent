# #945 方案依据与根因：AC4 焦点归还竞态 + A/C 例平台中立改写

- 状态：施工中
- 票据：#945（#919 第 5b 轮残余后补票；`:172` 家族 + A/C 例 nt 覆盖债）
- 分支：`codebuddy/945-testing-followup`（基点 `origin/main` = `c3c4a908795aa37a056368a4fcb53bb2588c2671`）
- 范围：**只动测试与必要加固**（`web/src/components/SessionList.tsx` 的焦点归还一处、
  `web/e2e/r-project-groups.spec.ts` 回归例、`tests/attachments/test_local_byte_store.py` 两例）。
  不重构产品、不改桌面端、不做孤儿 GC。

## 0. 方案依据（AGENTS §6.1）

两条独立机制各自 ≥2 个独立来源（代码来源用**本地浅克隆 + `file:line` + commit**）。

### 0.1 T2 机制：Radix 关闭菜单时的「焦点归还」是一个延后的 `trigger.focus()`

| 来源 | 版本 / commit | 文件:行 | 读到的事实 | 判定 |
| --- | --- | --- | --- | --- |
| 本项目实际运行的 vendored 包（**运行时权威**） | `@radix-ui/react-dropdown-menu@2.1.24`、`@radix-ui/react-menu@2.1.24`、`@radix-ui/react-focus-scope@1.1.16`（`web/package.json` 锁 `^2.1.24`） | `react-dropdown-menu/dist/index.mjs:114-115` | `onCloseAutoFocus` 的**默认**实现是 `if (!hasInteractedOutsideRef.current) context.triggerRef.current?.focus();` —— 关闭时把焦点**归还给 trigger** | **REUSE（直接复用其事件）** |
| 同上 | 同上 | `react-menu/dist/index.mjs:270` | `onUnmountAutoFocus: onCloseAutoFocus` —— 把 `onCloseAutoFocus` 接到 FocusScope 的卸载钩子上 | 承上 |
| 同上 | 同上 | `react-focus-scope/dist/index.mjs:94-101` | 卸载时在 **`setTimeout(...)`** 里派发 `focusScope.autoFocusOnUnmount`（`AUTOFOCUS_ON_UNMOUNT`）—— 归还焦点是**异步**的 | 竞态来源 |
| 上游源码（**独立第二来源**，浅克隆 `~/research-radix`，MIT） | `radix-ui/primitives` commit `01259a024d82ab3892d1e5938b1a50bb352c6df5`（2026-10-08） | `packages/react/dropdown-menu/src/dropdown-menu.tsx:192-193` | 与 vendored 逐字一致：`onCloseAutoFocus={composeEventHandlers(props.onCloseAutoFocus, (event) => { if (!hasInteractedOutsideRef.current) context.triggerRef.current?.focus(); })}` | 复核一致 |
| 同上 | 同上 | `packages/react/menu/src/menu.tsx:362,527`、`packages/react/focus-scope/src/focus-scope.tsx:191-199` | `onCloseAutoFocus?: FocusScopeProps['onUnmountAutoFocus']` / `onUnmountAutoFocus={onCloseAutoFocus}` / 卸载 `setTimeout` 派发 —— 与 vendored 链一致 | 复核一致 |
| 同上（**修法先例，直接抄**） | 同上 | `packages/react/menu/src/menu.tsx:1256-1259` | Radix 自己在嵌套子菜单里就用了本修法，注释原文：*"The menu might close because of focusing another menu item in the parent menu. We don't want it to refocus the trigger in that case so we handle trigger focus ourselves."* ⇒ `onCloseAutoFocus={(event) => event.preventDefault()}` | **COPY DESIGN（原生先例）** |

**为什么不是别家**：本项目下拉菜单只有 Radix 一处；焦点归还语义由 Radix 定义，「去别家找 CSS/焦点库」不会得到更强的证据。第二来源取**上游同源仓库的 commit**（vendored 是"实际跑的字节"、上游是"权威出处"），两者互校即可证"我理解的机制 = 真实生效的机制"。

### 0.2 T3 机制：Windows 上「只读」只有 FILE_ATTRIBUTE_READONLY 这一态

| 来源 | 版本 / commit | 文件:行 | 读到的事实 | 判定 |
| --- | --- | --- | --- | --- |
| 本机 CPython 标准库（测试实际运行的解释器） | CPython 3.12.3（`/usr/lib/python3.12/`） | `stat.py:183` | `FILE_ATTRIBUTE_READONLY = 1` —— Windows 只读位就是这一个属性 | **REUSE** |
| CPython 官方文档（**独立第二来源**） | `docs.python.org/3/library/stat.html`（`stat` 模块） | 「`stat.FILE_ATTRIBUTE_READONLY`」节 | 原文：*"On Windows, the following file attribute constants are available for use when testing bits in the **`st_file_attributes`** member returned by `os.stat()`."* —— Windows 的权限观测点就是 `st_file_attributes`，不是 `st_mode` 的低位 | 复核一致 |
| 本仓库既有先例（同文件自身的 nt 感知模式） | 本票基点树 | `tests/attachments/test_local_byte_store.py:107-112`（`_object_is_read_only`）、`:281-293`（`test_object_permission_is_read_only`） | 注释原文：*"Windows 没有 POSIX 权限位：CPython 按只读属性合成 st_mode（只读 ⇒ 0o444、可写 ⇒ 0o666），`os.chmod(path, 0o400)` 在 Windows 上不可满足 ⇒ 用原生 FILE_ATTRIBUTE_READONLY 更精确。"* | **REUSE（同文件模式）** |

> vendored 路径按 **pnpm 布局**解析：`react-menu` / `react-focus-scope` 是传递依赖，不在顶层
> `web/node_modules/@radix-ui/` 下，实际位于
> `web/node_modules/.pnpm/@radix-ui+<pkg>@<ver>…/node_modules/@radix-ui/<pkg>/dist/<file>`；
> 顶层 `web/node_modules/@radix-ui/react-dropdown-menu/dist/…` 可直接打开（直接依赖）。

**License 结论**：`radix-ui/primitives` 为 MIT；只**复用其公开事件 API 的用法**（`onCloseAutoFocus` 的 `preventDefault`），不复制源码行。CPython 为 PSF 许可；只引用文档事实与常量。

## 1. T2：`:172` / `:173` 家族根因（受控观测，非 flake）

### 1.1 现象（#919 第 5b 轮首跑）

用例 `web/e2e/r-project-groups.spec.ts:141:1 › AC4…`，失败在 `:172:25`
（`expect(page.getByLabel('项目名')).toBeVisible()`，timeout 5000ms，call log `element(s) not found`），
紧随其后的 `:173`（同输入框的 `toHaveAttribute('aria-invalid','true')`）。**两个断言之间输入框凭空消失**。

### 1.2 受控观测（探针「不入库」，原件留档 `~/workspace/system/dispatch/945-evidence/945-probe.spec.ts`）

- **probe1（确定性机制）**：空白名 Enter 后错误提示 `.project-rename-error` 可见；对活动元素调用
  `blur()` ⇒ `.project-rename-input` 与 `.project-rename-error` **同时从 DOM 消失**（`{input:1,err:1} → {input:0,err:0}`）。
  即 `InlineRename.onBlur` 把"值 trim 成空"当作取消，**整块编辑态卸载**——正是 `element(s) not found`。
- **probe2（焦点轨迹）**：打开重命名后焦点日志显示 Radix 关闭菜单把焦点**归还到 trigger**
  （`IN BUTTON.icon-btn[项目「项目 alpha」操作]`），随后输入框 `autoFocus`；正常时序下归还**先于**录入。
- **probe3（故障注入 = 确定性复现）**：把 trigger 的 `.focus()` 延后 1500ms（模拟负载下归还晚到）
  ⇒ 空白名 Enter 后等 2.2s，`inputCount = 0`（编辑态被延迟的归还 blur 掉、卸载）。**与 `:172` 签名逐字一致。**

### 1.3 根因链（代码面）

选中「重命名项目」→ `setRenamingId` 就地挂载 `InlineRename`（`autoFocus`）→ Radix 关闭菜单触发
`onCloseAutoFocus` → 默认实现把 `trigger.focus()` 放进 FocusScope 的 **`setTimeout`** 里（§0.1）
→ 该 `setTimeout` 与输入框的 `autoFocus`/用户按键是**同帧竞态**；负载越高它越可能晚到
→ 晚到时输入框被 blur → `InlineRename.onBlur` 判为取消 → 编辑态卸载 → `:172`/`:173` 断言的定位器消失。

**结论**：这是**产品层的焦点竞态缺陷**（不是纯 flake），`:172`/`:173` 是同一个家族（同一根因、相邻断言）；
`web/src/components/SessionList.tsx` 与 `ProjectDialogs.tsx` 在**改前（= 本票基点 `c3c4a908`）**与 `origin/main` 逐字节相同
⇒ #919 第 5b 轮的机械取证（"与 main 相同"）恰好说明缺陷**本就在 main 里**，只是负载下才现形。

## 2. T2 修法（奥卡姆剃刀 + 抄 Radix 原生先例）

`SessionList.tsx`：选中「重命名项目」时把一个 ref 标记为"焦点已主动交给行内编辑器"，
在 `DropdownMenu.Content` 的 `onCloseAutoFocus` 里**只对这一次** `event.preventDefault()`
（跳过归还；其余菜单项保持 Radix 默认归还）。与 Radix 嵌套子菜单自己的做法同形（§0.1 末行）。

- **不改产品语义**：其它菜单项（开对话框 / 纯动作）行为不变。
- **消融红证**：新增回归例 `r-project-groups.spec.ts` 的
  `#945：菜单归还焦点的竞态不得取消行内重命名（编辑态必须存活）`——把归还 `focus()` 确定性延后 1500ms。
  修复前该例 **red**（`expect(rename).toBeVisible()` 失败于 `:247`，签名同 `:172`）；应用本修法后 **green**。
  A/B 均实跑（见 §5 读数）。

## 3. T3：A/C 例平台中立改写（复用同文件 nt 感知模式）

两例的 nt skip 由 `aaec51fc` 引入（该笔一次给 3 例各补一条 nt 守卫，本票移除其中的 A、C 两例；
第三例 `test_discard_without_readonly_receipt_never_chmods` 保持 POSIX 专用）。根因是**判据/建场用了 POSIX 独有观测**，不是产品缺陷。

### 3.1 A 例（P0）`test_discard_does_not_touch_objects_it_did_not_clear`

- **原 skip**：`POSIX 专用：Windows 的 S_IMODE 恒为 0o666`。
- **根因**：末句 `assert stat.S_IMODE(os.stat(other).st_mode) == 0o600` 在 Windows 上不可满足
  （CPython 由只读属性合成 `st_mode`，S_IMODE 只有 `0o444`/`0o666` 两态，§0.2）。
- **改写**：改用本文件自己的 nt 感知判据 `_object_is_read_only`——nt 侧的等价观测是
  **"未被翻成只读"**（全量扫再 chmod 的实现会把它一并翻成只读，正是本用例要拦的回归）；
  POSIX 侧**保持原精确 `0o600` 断言**（由 CI 覆盖）。
- **红证**：把生产 `discard_local_artifacts` 变更为"全量扫 `.attachments` 再 chmod"的错误实现 ⇒
  本用例 **red**、且失败在该断言；恢复实现 ⇒ **green**（A/B 实跑见 §5）。

### 3.2 C 例（P2）`test_discard_restores_global_object_when_receipt_is_a_legacy_inode`

- **原 skip**：`POSIX 专用：setup 直接 unlink 只读文件（Windows 抛错）`。
- **根因**：建场 `receipt.unlink()` 直接删只读 hardlink —— Windows 上抛 `PermissionError`。
- **改写**：建场先清只读位再删（沿用生产同一写法 `os.stat(...).st_mode | stat.S_IWUSR`，
  见 `_read_only_retry_handler` `local_artifact.py:618`），删后把（此刻仍是 hardlink 的）全局对象复位只读。
  断言侧已用 `_object_is_read_only`（nt 感知），不变。
- **红证**：受控实验（探针「不入库」）——在"Windows unlink 语义"（`_install_windows_unlink` 模拟）下，
  原建场 `receipt.unlink()` **抛 PermissionError**；改为先清只读位后 **成功**。承重成立。

## 4. 覆盖恢复账

| 例 | 改前（nt） | 改后（nt） | POSIX 面 |
| --- | --- | --- | --- |
| A 例（P0） | skip，负向权限断言覆盖归零 | **运行**，"未被翻成只读"等价观测 | 原精确 `0o600` 断言保留 |
| C 例（P2） | skip，legacy inode 场景零覆盖 | **运行**，建场 Windows 安全 | 建场语义等价、断言不变 |

两例在 POSIX（CI）上**不再多/少跑**（skip 只作用于 nt），实跑 36 passed / 0 skipped 与基线一致。

## 5. 读数（本地 POSIX，非 Windows）

- A/C 两例：`2 passed`（改后）；整文件 `36 passed, 0 skipped`。
- A 例消融：错误实现下 `1 failed`（命中该断言）；恢复实现 `2 passed`。
- C 例消融探针：原建场 PermissionError（红）/ 清位后成功（绿），`2 passed`。
- e2e 回归例：修复前 `1 failed`（`:247`）/ 修复后 `1 passed`（chromium-1280，1 worker）。

## 6. 待办（用户侧，不自行声称）

- **票面要求 4 的「本机（Windows）全绿」**：Agent 在 Linux 服务器上，只能做平台中立改写 + CI（POSIX）
  两面验证；**Windows 本机全绿需用户在 Windows 机上实跑确认**（`pytest tests/attachments/test_local_byte_store.py`
  应 36 passed / 0 skipped）。列为待办上报，不自行声称已绿。
- `:173` 独立确认：与 `:172` 同族同根因（相邻断言、同一输入框），本票回归例已同时覆盖编辑态存活
  （含 `aria-invalid`），据此并案确认。

## 7. 独立双轴审查（fresh 会话，§8.8.1；范围 `c3c4a908..d7b2be14`）

两轴均 `APPROVE-WITH-FINDINGS`，**无 P0–P2**：

- **Standards 轴**：P3×3、P4×3，全为文档准确性（`stat.py` 行号 `172`→`183`、vendored pnpm 路径可解析性、
  §3 残留草稿片段、`test_object_permission_is_read_only` 定义行、`origin/main` 逐字节措辞、ref 不变量注释）。
- **Correctness 轴**：P3×2、P4×2 —— 共享 ref 的不变量（与 Standards 同点）、同类「菜单关闭归还 vs 挂载即
  autoFocus 浮层」竞态未覆盖（`SessionList.tsx` 的「新建任务」「加入项目」路径，未复现失败）、Windows nt 分支
  无 CI 覆盖（见 §6）、审查者受只读约束未能亲测"改前红证"（依源码机制核对 + 修复后绿 + §5 探针）。

**处置（P0–P4 全修）**：文档 5 条、代码注释 1 条（`focusMovedIntoRename` 不变量）随修笔闭合；
Correctness P3（同类竞态）**登记为残留**（不在本票 scope，未复现失败）、P4（CI/只读约束）为如实记录。
修笔 commit 见 tracker/台账行。
