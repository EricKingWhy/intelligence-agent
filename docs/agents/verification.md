# 验证车道表（改了什么 → 跑哪条 → 期望证据）

> **这份文件的用途**：把"这次该跑什么"从**记忆**变成**查表**。
> 起因：SDD 的完整门禁一次是分钟到十几分钟级，而"改一个简单小功能也要等一小时"里，
> 大量时间花在与本次改动无关的检查上；同时反向的事故也在发生——**该跑的没跑**
> （例如 `scripts/check_review_coverage.sh` 长期依赖 coreutils、在本机从未真的执行过）。
>
> 权威来源：`AGENTS.md` §14.10（集成前门禁清单）、`docs/SDD_WORKFLOW_PROTOCOL.md` §7
> （逐票验证与集成门禁）。本文只做**映射**，不新增门禁。

---

## 0. 一句话

```bash
# 推送前（≤60s，机械项）
python scripts/gate0.py          # 或让 .githooks/pre-push 自动跑

# 集成前（完整门禁，分钟级；一个冻结树只跑一次）
```
完整门禁 = §2 的**全部机械车道 ①–⑪**（`AGENTS.md` §14.10 清单 + 协议 §7 第 6 条点名的工具）；
其中**长耗时**的那几条（②③⑤⑩⑪）命令见对应小节（④ `tsc -b` / ⑦ 生成物守卫是秒级、且已在 Gate-0 里；⑤⑩ 本文件未在本批实测耗时）。**Gate-0 ≠ 完整门禁**（见 §4）。

---

## 1. 决策表

| 改了什么 | 至少跑 | 谁跑 |
| --- | --- | --- |
| 任何文件 | ⑨ `git diff --check` + ⑧ 覆盖闸门 | Gate-0（自动） |
| `src/**` 的 `.py` | ① ruff + ② 后端全量 pytest | Gate-0 跑①；②在冻结树 |
| `session/event.py` 或任一生成物 | ⑦ 生成物同步守卫（**必跑**，跨 `src`↔`web` 的有唯一一条） | Gate-0（自动） |
| `web/**` | ④ tsc + ⑤ vitest + ⑥ oxlint | Gate-0 跑④⑥；⑤按需 |
| `web/**` 的交互 / 渲染 | ⑪ playwright e2e（+ 必要时 ⑫ 真机验收） | 人工 |
| 任何"要进 main"的批次 | ①–⑪ + 两轴独立审查 | 人工，冻结树 |
| 只想重跑失败的那一条 | `python scripts/gate0.py --only <lane>` | 人工 |
| 一次改动只重跑受影响的那些 | `python scripts/gate0.py --affected <rev>`（见 §2 ⑭；<rev> 亦可为 `A..B`；**只内联 pytest 子集**） | 人工 |

---

## 2. 车道清单

实测耗时 = 2026-09-22 在本仓（tip `22aa291`、win32、`.venv` 就绪）当次读数；
**热缓存**指 `.ruff_cache` / `tsconfig.tsbuildinfo` 已存在。未实测的一律标注，不填数字。

### ① 后端静态检查 — `ruff check .`

- 命令：`.venv/Scripts/ruff.exe check .`（仓库根）
- 期望证据：`All checks passed!`，退出 0。
- 实测：冷 3.3s / 热 0.5s。
- 注意：**仓库没有任何 ruff 配置文件**（`ruff.toml` / `.ruff.toml` / `[tool.ruff]` 全无）；
  生效的是 ruff **上游默认规则集**，而 0.16.3 的默认集已经很宽（实测 `--isolated` 下
  一个 4 行探针即触发 `UP009`；`S110` / `BLE001` / `PLW1510` / `FURB188` / `PIE810` 也在默认集内）。
  ⇒ "牙齿"够用，但**强度随 ruff 版本漂移**（`ruff>=0.16.3` 无上界）；升级 ruff 时若本车道突然变红，
  那是规则集变了，不是代码变差了。

### ② 后端全量测试（冻结树，只跑一次）

- 命令（用户 2026-09-21 指令，协议 §7 第 6 条）：
  `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q -p no:randomly`
- 期望证据：`N passed / M skipped / 0 failed / 0 errors` **连同**跑它的 sha 与 `git rev-parse <sha>^{tree}`。
- 耗时：分钟级（历史读数见 `docs/phase_status/2026-09.md`，本批未重跑）。

### ③ 后端全量（本机沙箱绕行）

- 命令：`./scripts/run_tests_clean.sh [target]`
- 为什么：本机 WorkBuddy 沙箱经 `PYTHONPATH` 注入 `sitecustomize.py`，拦截 `Path.unlink()`
  并维护**跨测试累积**的删除配额；全量 pytest 后期配额耗尽 ⇒ 若干 `tests/evaluation/*`
  "随机"失败（`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`）。该脚本把 `PYTHONPATH` 清空。
- 期望证据：同 ②。

### ④ 前端类型检查 — `tsc -b`

- 命令（`web/`）：`node node_modules/typescript/bin/tsc -b`
- 期望证据：**无输出**、退出 0（`tsc` 只在出错时说话）。
- 实测：冷 22.7s / 热 10.8–14.7s。**不写进工作树**（`tsbuildinfo` 不产生未跟踪文件，已实测）。
- 注意：这是**类型检查**，不含打包。⑩ `vite build` 是另一条。

### ⑤ 前端单测 — `vitest run`

- 命令（`web/`）：`node node_modules/vitest/vitest.mjs run`
- 期望证据：`Test Files … / Tests N passed`。
- 耗时：未在本批实测（按 spec 数规模判断为十秒级，不要在这里写死数字）。
- 性能专用配置：`node node_modules/vitest/vitest.mjs run -c vitest.perf.config.ts`。

### ⑥ 前端 lint — `oxlint`

- 命令（`web/`）：`node node_modules/oxlint/bin/oxlint`
- 期望证据：退出 0；配置在 `web/.oxlintrc.json`（`react` / `typescript` / `oxc` 插件）。
- 实测：冷 4.4s / 热 0.6s。

### ⑦ 生成物同步守卫 + 验证映射守卫（跨 `src` ↔ `web`，只有这一条能抓）

- 命令：`PYTHONUTF8=1 PYTHONPATH= .venv/Scripts/python.exe -m pytest
  tests/test_event_types_generated.py tests/test_event_vocabulary_generated.py
  tests/test_verification_map.py -q -p no:randomly -p no:cacheprovider`
- 守卫对象：`src/agent_harness/session/event.py`（词汇**唯一真值**）→ 生成物
  `web/src/generated/event-types.ts`（`scripts/gen_event_types.py`）与
  `docs/EVENT_VOCABULARY.md`（`scripts/gen_event_vocabulary.py`）。
- 期望证据：`6 passed`（生成物 2 文件）+ `16 passed`（验证映射守卫，见 §2 ⑭）= **`22 passed`（3 文件）**。
- 实测：3 文件 22 例 —— pytest 自身热 **1.2s**；Gate-0 车道口径 **4.0s**（含解释器启动）。
- 漂移时先跑生成器：`uv run python scripts/gen_event_types.py` / `... gen_event_vocabulary.py`。

### ⑧ 审查覆盖闸门

- 命令：`.venv/Scripts/python.exe scripts/check_review_coverage.py`
  （`--list` 只打印三元组、有缺口退 2；`LEDGER=<path>` 可换台账）
- 期望证据：`提交总数 X / 已审查 Y / 待判定 Z`，且每条待判定 commit 都有归属
  （审查行 / `[whitelist]` 的 docs-only / 恰好只改台账），最后打印
  `✅ 台账覆盖闸门通过：<base>..HEAD …`，退出 0。
- 实测：**3.6–7.1s**。
- ⚠ 台账从**工作树**读（不读 HEAD 版）⇒ 必须在**干净检出**上跑。
- ⚠ 它是**声明式**闸门：只证明"每条 commit 都有归属"，**不证明审查真实发生过**——那由人对账
  （协议 §7 第 8 条的信任边界）。
- 参考实现：`scripts/check_review_coverage.sh`（**语义参考、冻结**——协议按行号引用它的
  `DOC_PATTERN` 与 fail-closed 形状；但它依赖 coreutils，在缺 coreutils 的 sh 下跑不动）。

### ⑨ 空白 / 冲突标记

- 命令：`git diff --check`（工作树）；查提交范围用 `git diff --check <base>..HEAD`。
- 期望证据：无输出、退出 0。实测：<1s。

### ⑩ 前端构建 — `vite build`

- 命令（`web/`）：`node node_modules/vite/bin/vite.js build`
- 期望证据：构建成功、产物在 `web/dist`。耗时：未在本批实测（秒到十秒级）。

### ⑪ 前端 e2e — playwright

- 命令（`web/`）：`node node_modules/@playwright/test/cli.js test --workers=2`
- ⚠ 端口纪律：`web/playwright.config.ts` 写死 `:5173` 且 `reuseExistingServer: false` +
  `vite --strictPort` + `scripts/preflight-port.mjs` 预检。**若验收用的 dev server 占着 5173，
  本车道会拒绝启动**（这是有意的：#209 实测复用了别人的 server ⇒ 18 failed 假红）。
- ⚠ 真机验收车道的前提（`:8000` 上是哪个 clone、`/api/capabilities` 期望）见
  `docs/ACCEPTANCE_LANE_ENV.md`；对着错的后端跑会得到**假阴性**。

### ⑫ 真机验收（浏览器证据）

- 需要"看得见的证据"（控件可达性、渲染条件）时走 `docs/ACCEPTANCE_LANE_ENV.md` 的车道。
- 判据不是"页面能打开"，而是该文件 §2 的 `/api/capabilities` 期望与
  `docs/ACCEPTANCE_CONTROL_INVENTORY.md` 的控件清单对账。

### ⑬ Gate-0（聚合入口，**不新增门禁**）

- 命令：`.venv/Scripts/python.exe scripts/gate0.py`
  （`--since <rev>` 额外查该范围的空白/冲突标记并报告改动面；`--only <lane>` 单条重跑；`--list` 列车道）
- 内容：⑨ + ① + ⑥ + ④ + ⑦ + ⑧，共 6 条。
- 期望证据：`Gate-0 PASS：6/6 通过，墙钟 <秒数>` + 首行 `tip=<sha> tree=<tree>`。
- 实测：**热 20–22s、冷 ≈40s**（预算 60s）。
- 由 `.githooks/pre-push` 在推送前调用（启用：`git config core.hooksPath .githooks`）。

### ⑭ 验证映射与 `--affected`（受影响面的机器化；issue #292）

- **产物**：`docs/agents/verification.map.tsv` —— 「代码面 ↔ 必跑车道 / focused 用例」的**机械映射**。
  列**对齐** `docs/agents/skills/create-verification-skill` 的 feature 四要素（`Sub-features` /
  `How to get to it` / `Driving it with <harness>` / `Gotchas`），另加 `surface`（只允许**路径前缀**或
  **精确路径**，禁通配、禁 catch-all）与 `neg_tier`（`blast-radius` 确定性阶梯）。
- **守卫**：`tests/test_verification_map.py`（跑在 Gate-0 的 `guards` 车道里）：① `git ls-files` 里
  **每个**被跟踪文件都被映射；② 结构合法（7 列 / layer 唯一 / 车道 id 在词表内 / focused 路径存在）；
  ③ **每一行都承重**（删掉任一行 ⇒ 至少一个文件的受影响集合变化）；④ 自带**独立**匹配器，与
  `gate0.py` 对**每个**文件求值**逐项相同**；⑤ `focused` 必须**真跑得动**——pytest 目标要对盘核到
  `test_*.py`（`pytest <空目录>` 会以 exit 5 收场，与 vitest 的 `No test files found` 是同一形状的
  **假 FAIL**），且"哪些 focused 按设计不内联"必须在 `test_focused_only_inlines_pytest_subsets` 里
  **逐个登记**。⇒ 映射腐烂、或把非 pytest 面接上内联车道 = **推送前就红**。
- **命令**：`.venv/Scripts/python.exe scripts/gate0.py --affected <rev>`（`<rev>` 亦可为范围 `A..B`）。
  只跑受影响车道 + 受影响 focused 用例；**默认行为不变**（不带它恒跑全部 6 车道）。
- **⚠ `focused` 的内联范围（2026-09-22 定，两条血证）**：**只有 pytest 子集内联**（`tests/**` 或 `*.py`）。
  前端 / 浏览器类（`vitest` / `e2e` / `live`）与整个 `tests/`（= `pytest-full` **本身**，子集才便宜）
  **一律只登记、不内联**，输出里逐条注明原因。血证 ①（坐标系混用）：`focused` 路径一律**相对仓库根**，
  而 vitest 的 cwd 是 `web/` ⇒ 把 `web/src` 原样当 filter 会 `No test files found, exiting with code 1`
  ——**假 FAIL**，而那次改动根本没碰 `web/`。血证 ②（潮水线以下的红）：修好过滤器后测出，**干净 HEAD**
  上 `vitest run src` 本身就是红的（67 文件 1067 例中 1 例超时，`web/src/components/StepDetail.window.test.tsx`，
  即 B-29 已知 flake）⇒ 前端红**无法归因**到本次改动。
- **期望证据**：`受影响面（--affected …）` 块 + `Gate-0 PASS/FAIL`；`neg_tier < 4` 的层会被标 **unproven**。
- **实测（2026-09-22，同树 `HEAD=784df9e / tree=fb8780bd`）**：

  | 状态 | 改动面 | 选中车道 | 内联跑的 | 只登记不跑的 | 墙钟 |
  | --- | --- | --- | --- | --- | --- |
  | A 后端 session | 1 文件（`session/approval.py`） | `coverage/diff-check/guards/pytest-full/ruff` | 4 条快车道 + `tests/session` | `pytest-full` | **107.7s**（其中 `tests/session` 96.8s） |
  | B 前端 src | 1 文件（`web/src/**`） | `build/coverage/diff-check/oxlint/tsc/vitest` | `diff-check/oxlint/tsc/coverage` | `web/src`（vitest）、`build` | **12.2s** |
  | C 纯 docs | 1 文件（`review_ledger.tsv`） | `coverage/diff-check` | 这两条 | — | **3.8s** |

  同树**全量** Gate-0 作对照：6/6 PASS **16.3s**。⚠ A 行比全量 Gate-0 **更慢**，这是**正确**的——
  全量 Gate-0 **一条测试都不跑**，而 A 真的跑了 96.8s 的受影响子集。`--affected` 的价值**不在**"比快车道快"，
  而在 ① 告诉你**哪些重车道**受影响（A 会点名 `pytest-full`，B 会点名 `vitest` + `build`）
  ② 用**受影响子集**替掉整套 `pytest-full` / `vitest`。

- **"整条重来"的对照基线（同树实测）**：全量 Gate-0 **16.3s** + 全量 `pytest tests` **511.2s**
  （**3098 passed / 3 failed**，3 条**全**在 `tests/evaluation/*` —— 即 §3 里登记的 safe-delete 配额假红，
  与本次改动无关；这恰好又一次说明 §5 第 5 条"只看失败**集合差集**"）+ 全量 `vitest run src` **28.2s**
  ≈ **555.7s**（还不含 ⑩ 构建 / ⑪ e2e）。⇒ A 状态用 `--affected` 只花 **107.7s**，**约 5.2× 便宜**，
  并且它点名了唯一必须补跑的重车道（`pytest-full`）。
- **变异证明（隔离克隆 `%TEMP%` 里真删真改，正控全绿）**：删 `frontend-src` 整行 ⇒ 覆盖面红；
  加一条重复行（`docs-dup`）⇒ **承重**红；把 `focused_runner` 复原成血证 ① 的写法 ⇒ **内联策略锁**红；
  `neg_tier` 改 9 ⇒ 词表红。
- ⚠ **边界**（与协议 §8.8.9 **同源**，改一处必须两处同改）：只用于**失败后的增量重跑**；
  未映射路径 ⇒ **fail-closed 退回全量**；不得替代推送前全量 Gate-0，也不得替代集成前完整门禁（§4）。

---

## 3. 本机环境（WorkBuddy 沙箱）的跑法差异

| 现象 | 原因 | 正解 |
| --- | --- | --- |
| `npm` / `npx` 秒退，退出码非 0，日志十几字节乱码（GBK 读出来是"拒绝访问。"） | 沙箱把 `cmd.exe` 拉黑，任何 `.cmd` / `.bat` 入口都起不来 | 直接调包的 `.js` 入口：`node node_modules/<pkg>/…`（见 §2 各条） |
| 脚本里 `dirname` / `wc` / `comm` / `grep` / `sort` / `mktemp` 全 `command not found` | bash shim 的 **PATH 里没有** coreutils（**不是不存在**） | 用 python 或 `git` 子命令。跑 `.sh` 时：把 `<PortableGit>/usr/bin` 加进 PATH，并**用全路径 bash**（`"…/PortableGit/…/bin/bash.exe" scripts/x.sh`）——直接写 `bash` 在部分调用上下文里会落到被安全策略拦下的 WSL 通道（实测 `PROGRAM BLOCKED … wsl.exe`），与脚本本身无关 |
| 全量 pytest 后期若干 `tests/evaluation/*` "随机"失败 | safe-delete shim 的跨测试删除配额 | `./scripts/run_tests_clean.sh`（清空 `PYTHONPATH`） |
| `os.symlink` 静默 no-op（不抛异常、不创建） | 沙箱文件保护 | 相关用例先探针再归因，别当成自己改出来的 bug |
| 分钟级脚本被 SIGTERM、且重定向文件是空的 | 前台跑长任务 + stdout 块缓冲 | 长跑放后台跑（`run_in_background`），或先落盘再读 |

`.gitattributes` 钉住 `*.sh` 的 `eol=lf` 与 `*.ps1` 的 `eol=crlf`（**不是**都 `lf`——2026-09-22 两轴审查指出此处笔误）；**无扩展名的 git hook**（`.githooks/pre-push`）
2026-09-22 已单独加规则——否则 `core.autocrlf=true` 会把它检出成 CRLF，`exit 1\r` 这种行直接坏掉。

---

## 4. Gate-0 与完整门禁的边界（别把前者当后者）

- Gate-0 **只覆盖机械可判项**（§2 的 ⑨①⑥④⑦⑧）。它**不覆盖**：② 后端全量测试、
  ⑤ vitest 全量、⑩ 构建、⑪ e2e、以及**任何语义 / 规格 / 边界 / 权限**问题。
- Gate-0 **推送前不做按路径跳过**：全量 6 车道 ≈20–40s，已满足预算；"按改动路径跳过某条车道"属于
  放松（跨层影响难以穷举），机制上没有必要。改动面只作信息展示。
- ⚠ **上面那条的适用范围被收紧（2026-09-22，issue #291）**：它**只**对**本节这 6 条机械车道**
  （`diff-check` / `ruff` / `oxlint` / `tsc` / `guards` / `coverage`）成立——理由是它们耗时已被实测
  （20–22s 热 / 36–45s 冷）、且判定是机械的。**不得**据此认为"完整门禁与 e2e 也可以按路径跳过"：
  **不在 Gate-0 里的那几条（②③⑤⑩⑪）**是长耗时的那批（② 后端全量 pytest 是分钟级；③⑪ 本文件无耗时读数；⑤ 标注十秒级、⑩ 标注秒到十秒级，两者均未在本批实测；⑫ 真机验收属人工车道，无耗时读数），且它们的受影响面**不由人当场划集合**（那正是放松的入口），只能来自机械可复核的
  映射（协议 **§8.8.2 INV-1 / §8.8.3 边界表 / §8.8.9**；映射**已机器化**，见 §2 ⑭）。
- **`--affected <rev>` 的边界（issue #292 **已落地**，见 §2 ⑭）**：它**只**用于**失败后的增量重跑**，既不得替代
  **推送前全量 Gate-0**（推送前恒跑全部 6 条），也不得替代**集成前完整门禁**（协议 §8.8.4 第 1 行）。改动面里出现**未映射路径** ⇒ **fail-closed 退回全量**；`neg_tier < 4` 的层会被标 **unproven**，**依据 unproven 主张跳过任一条车道必须在台账 / 落点记录里写明**。
- `pre-push` hook **是本地便利，不是安全边界**：`git push --no-verify` 可绕过；
  `core.hooksPath` 是**本地配置**、不随仓库分发 ⇒ **别的 clone 没启用就等于没有**。
  所以它取消不了 CI，也取消不了两轴独立审查；**两侧仍然没有 CI**（这是已知缺口，不是本文能解决的）。
- **fail-closed**：任何车道"工具缺失 / 超时 / 无法执行"一律算**失败**，不算"跳过"、不算通过
  （沿用覆盖闸门"核对不了就不放行"的口径）。

---

## 5. 读数纪律（协议 §7 / §8 的机械要求）

1. **任何门禁读数必须能指到它跑在哪棵树上**：记 sha **与** `git rev-parse <sha>^{tree}`。
   Gate-0 首行就打印这两项。
2. **不跨批沿用读数**；不传递来源树不明的读数（协议 §8.7）。
3. 例外通道：集成前先比 `HEAD^{tree}`——两 clone tree 相同即证明"跑过门禁的树 = 被集成的树"，
   不必重复跑全量（协议 §7 第 8 条）。
4. 失败时**只重跑失败的那条**（`--only`），不整条流水线重跑；整票级的受影响重跑用 `--affected <rev>`
   （见 §2 ⑭），但**它标出的 `unproven` 必须一并写进读数**。
5. 不要用"失败总数"归因：沙箱负载下非确定性，只看**失败集合差集**。
