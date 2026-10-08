# W-21 七票修复批（#848–#856）—— 逐票根因、修复与验证读数

**日期**：2026-10-08 ｜ **分支**：`fix/w21-windows-gate-fixes` @ worktree `D:\intelligence-agent-wt-w21`
**起点**：`412e4570`（证据件 54 落盘处）｜ **终点**：`3c850121`
**触发**：用户裁决「先修 #809 的六条 → 桌面开窗 / TUI 冷启动 → Run A / Run B → 才谈 B-5 与关单」，
以及「同意 #848 R1–R7、#849/#850/#851/#853/#854/#856 是否关单前修」。本轮**七票全部修完**，
`#365` 的 Run A / Run B 需要在修复后**重跑**（上一轮两次通过是修前冻结件 `01477e59…` 上的读数）。

> 逐票的根因叙述与实现取舍以**各 commit message 为准**（本仓的 commit message 就是第一手细节记录，
> 此处不重述，只给索引与**验证读数**）。

## 1. 逐票索引

| 票 | 症状（一句话） | 提交 | 主要文件 | 验证读数 |
| --- | --- | --- | --- | --- |
| #851 | CRLF 文件里「从 read 结果逐字抄来」的 `old_string` 恒 `count()==0`，报「未找到匹配的字符串」 | `3ef9f8de` | `src/agent_harness/tools/_line_endings.py`(新) / `apply_patch.py` / `edit.py` / 两个测试文件 | `tests/tools/` **182 passed 1 skipped**；ruff 干净 |
| #850 | 多行 `bash` 命令在 Windows 上被 `cmd.exe` 引号剥离规则吃掉，却报 `exit_code=0 + outcome=success`，模型据此写假报告 | `c4860324` | `sandbox/base.py`(契约层异常) / `sandbox/local.py`(判据) / `tools/bash.py`(映射) / 两个测试文件 | `tests/sandbox`+`tests/tools` **328 passed 15 skipped**；ruff 干净 |
| #853 | 对**空闲**会话发消息，`POST /messages` 返回 SSE 而 TUI 无条件 `JSON.parse` ⇒ `send failed: SyntaxError`（消息其实已受理并跑完） | `1be41f56` | `tui/src/api.ts` / `tui/test/api.test.ts`(新) | tui `tsc --noEmit` 干净；tui **63 passed 0 failed** |
| #854 | 空闲附着的 TUI **一帧都收不到**直播事件（服务端 `sse-starlette` 发 `\r\n\r\n`，客户端只认 `"\n\n"`） | `cfb1c71a` | `tui/src/sse.ts` / `tui/test/sse.test.ts` | 单元红→绿（用真实服务字节）；端到端：修前 0 帧，修后 **8 帧 seq 9–16，首帧在 POST 后 540 ms** |
| #849 | W-20 判定器把「前一次探索留下的脏库」当输入 ⇒ 判定结果取决于探索顺序（同一份正确修复，先跑后跑结论相反） | `cc7b5994` | `tools/challenge-fixture/judge.py` / `README.md` / `tests/challenge/test_judge.py` | 红测试的 `missing` 列表与票面实测清单逐项一致；对真实 passing app 在**污染库**上跑：旧判定器 `fail`（同一清单）、新判定器 `pass` |
| #856 | 仓库根 pytest（车道 ② 的原样命令）走进两棵 gitignored 打包产物树 ⇒ **零用例**的解释器访问违例，且无任何失败签名 | `6535a4d5` | `pyproject.toml` / `tests/tooling/test_pytest_root_scope.py`(新) / `docs/agents/verification.md` | 修前 `rc=139` 零用例 → 修后 **6768/6819 collected (51 deselected) in 9.52s, rc=0**；显式 `pytest .` 同读数 |
| #848 | 修后重审登记的 R1–R7（tint 扫源回归 / 探针钉错对象 / 预算注释漂移 / 子进程退出未接 / 文案秒数矛盾 / 启动器守门不钉顺序 / 扫源窄口） | `3c850121` | `tui/test/tints.test.ts` / `tui/src/host.ts` / `tui/test/host.test.ts` / `desktop/src/service-host.ts` / `desktop/test/installer-tui.test.mjs` | 见 §3；tui **69/69**，desktop **160/161**（唯一红为在册的 staging 陈旧，见 §4） |

## 2. 红证据 / 变异证据（逐条，可复跑）

**#856（真机复现，两条腿）**

```text
修前：PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q -p no:randomly --collect-only
      Windows fatal exception: access violation
        File "...\desktop\dist-installer\win-unpacked\resources\python\Lib\site-packages\
              win32comext\taskscheduler\test\test_addtask.py", line 23 in <module>
      rc=139（Git Bash；cmd 下 3221225477 = 0xC0000005）  收集到 0 个用例
修后：同一条命令 ⇒ 6768/6819 tests collected (51 deselected) in 9.52s   rc=0
      显式 `pytest --collect-only .` ⇒ 6768/6819 (51 deselected) in 13.83s  rc=0
```

新守卫 `tests/tooling/test_pytest_root_scope.py` 的 red→green：red 时 `config.args ==
['D:\\intelligence-agent-wt-w21']`（= 整个 rootdir）、`--ignore == []`；green 时收集面落在
`tests/` 内、两棵产物树都在 `--ignore` 里。该测试用 pytest **自己的** `Config.parse([])` 读
「这次调用实际会收集什么」，不做任何收集。

**#848 R1/R7（同一份 fixture 上，旧判据 vs 新判据）**

```text
fixture：三种引号各一条 + `'a"rgb(10, 11, 12)"'` + `"1px solid rgb(13, 14, 15)"` + `"#ffff"`
旧判据（整串就是一个颜色，交替正则取字面量）：找到 3/6
  漏：a"rgb(10, 11, 12)"（R1 嵌套）、1px solid rgb(13, 14, 15)（R7 嵌入）、#ffff（R7 四位 hex）
新判据（三次 matchAll 串联 + 在值里搜颜色 token）：找到 6/6
```

**#848 R2/R3/R4/R5/R6（变异红证）**

| 变异 | 期望 | 实测 |
| --- | --- | --- |
| `scanColorLiterals` 改回内联双引号匹配（M2） | `tui/test/tints.test.ts` 红 | **红**；还原后绿 |
| 桌面 `DEFAULT_START_BUDGET_MS` `90_000`→`60_000` | 跨包预算契约测试红 | **红**（"两个客户端不许对同一个子进程各有一套启动预算"）；还原后绿 |
| 去掉 `onExit` 中止 + 还原旧文案 | 子进程死亡用例红 | **红**：拿到的正是登记的那条文案「90s 内未就绪…（冷启动实测 36-42 s）」；还原后绿 |
| 启动器捕获行移到 node 行**之前**（M3） | 行序守门红 | **红**（child at 24 / capture at 23）；还原后绿 |

**#854（端到端，真服务 + 真重连循环）**：修前用真实 `openStream` 重连循环**一帧都收不到**
（连 run #1 都没有）；修后 8 帧 seq 9–16、首帧在 POST 后 540 ms。单元测试用的是**抓下来的真实
服务字节**，不是构造的样例。

## 3. #848 逐条处置

- **R1**（P2）→ 三次独立 `matchAll` 串联（交替正则会在 `'` 处吞掉内嵌双引号）。已修。
- **R2**（P2）→ 扫源抽成 `scanColorLiterals(dir)`，探针扫 fixture 目录、与真实扫描同函数。已修。
- **R3**（P3）→ 注释里的秒数**删掉**（不再复述会漂移的数字）+ 跨包预算契约测试。已修。
- **R4**（P3）→ `SpawnedServe.onExit` + `waitForService` 的 `abortReason` 中止，与桌面壳对齐。已修。
- **R5**（P3）→ 超时文案去掉写死秒数；`START_BUDGET_MS` 注释把两条读数按场景并排记录。已修。
- **R6**（P3）→ 启动器新增**行序**断言（两处模式行首锚定）。已修。
- **R7**（P4）→ 判据改成「在字符串值里搜颜色 token」，收进嵌入颜色与 4 位 hex。**部分修**：
  注释里被引号包住的颜色仍会假红 —— 方向是 fail-closed（假红响亮、当场可改；假绿才会把
  崩溃放进安装件），消掉它要写 TS 注释剥离器，**登记不修**。

## 4. 本轮**未**转绿的一条（如实登记，与本批无关）

`desktop` 全量 `node --test` = **160/161**，唯一红是
`assertStagedProductMatchesSource` 的「matches the checkout for the staged runtime on this
machine」：

```text
staged product differs from the checkout: sandbox/base.py, sandbox/local.py, sandbox/__init__.py,
tools/apply_patch.py, tools/bash.py, tools/_line_endings.py (missing), 7 file(s)
— run "python desktop/scripts/prepare_python_runtime.py" before packing
```

归因：`desktop/installer/staging` 的 mtime 是 **2026-10-07**（上一次打包 `01477e59…` 时准备），
而 `sandbox/*.py` / `tools/*.py` 在 **2026-10-08** 被 #850（`c4860324`）/ #851（`3ef9f8de`）改过。
这是**打包前置条件**（守卫按设计拒绝用陈旧 staging 打包），不是 #848 引入的缺陷；重打包前
按提示重铺 runtime 即绿。`desktop/dist` 已按新源 `npm run build` 重编，D10 的
`assertDesktopBuildFresh` 真树用例已绿。

## 5. 与 #365 的关系（未关单）

- 本批**只改代码与测试**，不改票面、不改验收标准；`#365` 的 Run A / Run B 两次独立完整通过
  是在**修前**冻结件上取得的，修复后必须**重跑**才谈 B-5 与关单。
- 七票修完后的顺序：记账（本文件 + tracker + 归档）→ 双轴独立审查 + review coverage →
  冻结树全量 13 车道 → 重打包 → Run A / Run B 各两次。
- 本批**顺带登记**的新缺陷：`#859`（`stream/truncated` 控制帧无 `time`，`parseEnvelope` 丢弃它
  ⇒ TUI 的截断重建路径不可达；Web 端不受影响）。按 Scope Lock 未在 #854 里顺手修。

## 6. 两轴独立发现审查（§8.2/§8.3；范围 `412e4570..3c850121` = 7 笔代码）

派单前按 §8.2 第 4 条在同一冻结 sha 上跑**裸全量** `python scripts/gate0.py`：**5/6 通过**，
唯一红是 `coverage`（结构性例外：审查行只能在审查之后写），其 ❌ 集合**恰为本批 7 笔**
（`3ef9f8de` / `c4860324` / `1be41f56` / `cfb1c71a` / `cc7b5994` / `6535a4d5` / `3c850121`），
❌ 里没有本批之外的缺口 ⇒ 判据「除 coverage 外全绿、且 coverage ❌ ⊆ 本批代码笔」成立，
机械面清零后才派单。读数：`docs/gate/3c8501213726cc226fe78fba7870cd1d09ce6886.json`
（tip `3c8501213726` / tree `3efca616cf54`，墙钟 32.0s）。

两轴各一独立只读子代理、互不可见。派单 prompt 按 §8.3 第 1 条给全预算：目标 sha + 9 个文件的
`git hash-object` 期望值、报告上限 40 行、墙钟上限 25 分钟、自写变异上限 2 条（且只允许在
**主工作树之外**的副本里做）、「无论跑到哪一步都必须给出结论行」，并写明 §8.3 第 7 条
（机械面已由 Gate-0 读数覆盖，不重复报）。两轴回报**均**：9/9 冻结值相符、**零文件改动**
（`git status --short` 只剩派单前既存的 3 个未跟踪 docs 文件）。

| 轴 | 结论 | P0 | P1 | P2 | P3 | P4 |
| --- | --- | --- | --- | --- | --- | --- |
| Correctness | PASS-WITH-FINDINGS | 0 | **1** | 0 | 2 | 0 |
| Standards / Spec | PASS-WITH-FINDINGS | 0 | 0 | 0 | 6 | 1 |

### 6.1 Correctness 轴的 P1（编排侧已独立复现）

`src/agent_harness/sandbox/local.py:57-64` 的判据是 `body = command.strip()`，把**前导**换行
也一并删掉，于是前导换行的命令不被拒绝：

```text
subprocess.run(c, shell=True) 实测（本机 win32，cmd.exe）
  "\necho A"        -> rc=0  stdout=''     stderr=''      ← 静默 no-op 却报成功（未被拒绝）
  "\n\necho A"      -> rc=0  stdout=''     stderr=''
  "  \necho A"       -> rc=0  stdout=''     stderr=''
  "echo A\necho B"   -> rc=0  stdout='A\n'  stderr=''      ← 内部换行（已被拒绝）只跑第一行
  "echo hi\n\n"     -> rc=0  stdout='hi\n' stderr=''      ← 尾随换行正常
```

⇒ 判据应当是 `rstrip()`（只去尾部），并把「前导换行 = 什么都不跑」「内部换行 = 只跑第一行」
两种形状在文案里分开说清。`tests/sandbox/test_local_sandbox.py` 的 5 条用例只覆盖内部换行与
尾随换行，**没有前导换行用例**。

### 6.2 其余 finding（登记，本轮不修）

- **tint token 正则与 5/7 位 hex**（两轴方向判断相反，编排侧实测**两种方向都存在**）：
  `tui/test/tints.test.ts` 的 `COLOR_TOKEN` 只列 3/4/6/8 位 hex，正则按左到右取首个可匹配分支
  ⇒ `"#12345"` 被截成 `#1234`（4 位，`parseColor` 拒 ⇒ **假红**）、`"#1234567"` 被截成
  `#123456`（6 位，合法 ⇒ **假绿**）。当前 `src` 里无触发，属潜在脆弱。
- **#851 行尾容忍 / #850 新契约异常无规格出处**：`05_SANDBOX_CODING_TOOLS.md:107` 仍写
  "exact old_string → new_string"，`sandbox/base.py` 新增的 `MultiLineCommandUnsupportedError`
  在 `05` §4 的「Sandbox Contract 保持薄」里没有对应条目；规则只写在 docstring。
- **#849 判定口径与冻结证据件分叉**：`judge.py` 新增 `reset_acceptance_artifacts()` 并把
  `check_retry_same_returns_existing` 改判 HTTP 200 + 40→40，而
  `docs/live_gate/w21/10-w20-deterministic-evidence.md:19` 仍记旧口径（历史证据不改写，需加指针）。
- **`tui/test/host.test.ts` 的 R3 契约测试读 `desktop/src/service-host.ts` 源文本**：把 TUI 套件
  绑到另一条打包线的源树（desktop 文件改名/缺席会让 TUI 套件红）。
- **`tools/edit.py:101/109` 的错误码**与规格 `05:110/112` 承诺的 `NOT_FOUND` / `AMBIGUOUS`
  不符 —— **非本批引入**，只登记 Gap。
- **#853 顺带把 3 处构造参数属性改成显式字段**：§8「不顺手重构」的边缘，但 Node 的
  strip-only TS 无法 import 参数属性，是让本票可测的必要前置，已在 commit message 披露。

## 7. 冻结树全量重车道（`3c850121`，串行跑，避免机器负载造假红）

跑前逐字节核对工作树：`git diff HEAD` 空、9 个被改文件 `git hash-object` 与 HEAD blob 全等、
`git diff --check` 干净 ⇒ 本轮读数取在**零改动窗口**内。
（**披露**：作者 #848 的变异红证发生在**冻结之前**，且当时确实改过主工作树；§8.3 第 2 条要求
「主工作树发生过变异 ⇒ 同窗口全量读数作废」—— 本轮读数的窗口起点在冻结与逐字节核对之后，
两者**不同窗口**，故读数不作废；变异证据只作作者红证，不作门禁读数。）

| 车道 | 命令 | rc | 结论 / 归因 |
| --- | --- | --- | --- |
| ① ruff | `.venv/Scripts/ruff.exe check .` | 0 | PASS |
| ② pytest 全量 | `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q -p no:randomly` | **1** | **6739 passed / 27 skipped / 51 deselected / 2 failed**，28:14。见 §7.1 |
| ③ `run_tests_clean.sh` | — | 未跑 | ③ 是 ② 的**本机沙箱绕行**（清空 `PYTHONPATH`，避开删除配额造成的 `tests/evaluation/*` 随机红）；本轮 ② 的两条失败都不在 `tests/evaluation`，该形状**没有出现** ⇒ 不需要绕行读数 |
| ④ web `tsc -b` | `node node_modules/typescript/bin/tsc -b` | 0 | PASS（41s） |
| ⑤ vitest | `node node_modules/vitest/vitest.mjs run` | **1** | 单独复跑（排除负载）：`Test Files 1 failed \| 100 passed (101)`、`Tests 1 failed \| 1496 passed (1497)`；唯一失败 = `src/components/StepDetail.window.test.tsx > … > DIFFS / ARTIFACTS：默认先裁`（5000ms 超时）= **在册 flake B-29**（2026-10-07 已有隔离豁免台账行）；本批 `web/` 改动 **0 文件**（`git diff --name-only 412e4570..3c850121 \| grep -c '^web/'` = 0）⇒ 非本批 |
| ⑥ oxlint | `node node_modules/oxlint/bin/oxlint` | 0 | PASS |
| ⑦ guards | 4 文件 pytest | 0 | PASS（11s） |
| ⑧ coverage | `python scripts/check_review_coverage.py` | 1 → **0** | 车道内的红是在途态（本批 7 笔尚无审查行）；写台账行后复跑 **rc=0**：提交总数 2975 / 已审查 2901 / 待判定 74，**0 条未归属** |
| ⑨ diff-check | `git diff --check` | 0 | PASS |
| ⑩ vite build | `node node_modules/vite/bin/vite.js build` | 0 | PASS（32s） |
| ⑪ playwright | `node node_modules/@playwright/test/cli.js test --workers=2` | **1** | **环境阻塞，本车道没有取得读数**：preflight 拒绝复用 5173（#209），该端口被**另一个 clone** 的 dev server 占着（PID 12528，`C:\Users\…\.codex\worktrees\issue-496-quality\intelligence-agent`）。按 §14.13 不干扰其他线，**未**结束该进程 |
| ⑫ 真机验收 | — | n/a | 需安装后的产物；本批未重打包（见 §8） |
| ⑬ Gate-0 | `python scripts/gate0.py` | 5/6 | 见 §6 首段；唯一红是 coverage 的结构性例外 |
| 补 · tui | `npm test` / `npm run check` | 0 | **69/69**；`tsc --noEmit` 干净 |
| 补 · desktop | `npm test` | 1 | **160/161**，唯一红 = `assertStagedProductMatchesSource`（staging 陈旧，见 §4），是重打包前置 |

### 7.1 本批**引入**的一条红（必须修）

```text
FAILED tests/tooling/test_approve_policy.py::TestExecutorIntegration::test_command_policy_newline_rejected_without_crash
  assert result.result.ok is True
  E  assert False is True   （ToolResult.ok=False，message=「命令被拒绝：Windows 本机沙箱用 cmd.exe 执行…」）
```

机械归因（三条只读证据）：该测试文件**不在**本批 diff 里（`git diff --name-only 412e4570..3c850121`
无此文件）；它最后一次改动是 `9fab6ff2`（#684 审批持久规则，早于本批）；守卫 `_has_interior_newline`
在基点 `412e4570` **不存在**（`git show 412e4570:src/agent_harness/sandbox/local.py | grep -c` = 0），
由 `c4860324`（#850）引入。⇒ 这条红是 #850 的行为变更打翻了**既有测试**里「多行命令仍能跑通」的
假设（该用例本意只测「含换行的命令级审批规则不装、不崩」，却把「命令能跑通」当成了断言）。

## 8. 结论与待裁决（**本批不可交付**）

- **`#365` 仍未关单**，且本批**没有**达到「冻结树全绿」：② 有一条**本批引入**的红（§7.1），
  Correctness 轴另有 1 条 P1（§6.1，编排侧已独立复现）。两条都在 #850 的面（`local.py` 的换行
  判据 + 被它打翻的既有测试）。
- 这两条**需要改代码/测试** ⇒ 会产生新提交 ⇒ 按 §8.3/§8.8.5 需要一轮「针对新 diff 的修后重审」；
  而 **#365 的「每轴 1 轮、无第二轮」额度已用满**（上一批 `e8b57282..779f8980` 用掉，台账行当时
  已写明「额度已用满 ⇒ 只登记不修」）。协议对这种情况的出口是**停止修复 + 登记残余 + 交用户裁决**，
  不是自动再开一轮 ⇒ 本轮**只登记不修**，等裁决。
- 因此本轮**未**重打包、**未**跑 Run A / Run B：在已知 P1 与已知红测试未处置前跑出来的
  「两次通过」不构成 #365 票面要求的证据（票面明令不得隐藏失败、不得只用 fake model 宣称通过）。
- ⑪ playwright 本车道**无读数**（被另一个 clone 的 dev server 占住 5173）——这是一处**已知缺口**，
  不写成通过；待端口空出或用户裁定后重跑。
