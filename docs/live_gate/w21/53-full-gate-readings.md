# W-21 全量门禁读数（V3.1-lite 重车道，冻结树）

> 目的：给 #365 的「跑当前 V3.1-lite 全量门禁」留一份**可复跑**的读数，并如实登记其中
> 两条**环境性**红与一条**既有契约漂移**红。本文件只记录读数与归因，不修产品代码
> （#365 票面「不在本票修产品代码」）。

## 0. 读数绑定

| 项 | 值 |
| --- | --- |
| 分支 | `fix/w21-windows-gate-fixes`（worktree `D:\intelligence-agent-wt-w21`） |
| 冻结安装件 | `desktop/dist-installer/Intelligence-Agent-Setup-0.1.0.exe`，**204,379,033 B**，sha256 `01477e59779693299216f9595966106e71a7f7d92409856aa18729899b8591e6` |
| 安装件对应提交 | `28a382cc`（tree `d8dfa93c52049a5c2166a8d247aa407321d016f7`） |
| 重车道运行 tip | `1cfc7b02`（tree `11c103d2339d03049998ccaa365ea7646181b87a`，`dirty=(clean)`） |
| Gate-0 运行 tip | `09d91216`（tree `e1c68e40e8c32400e4941991753321e0f4cd96b5`） |
| 代码子树同一性 | `src` = `e47efe9b08edd3ccac935024e11ca1e723ff0696`、`web` = `29f764d7e94f7be7db9fae2b206c521c6e93fdc6`，在 `28a382cc` 与 `09d91216` **逐字相同**；`git diff 28a382cc 09d91216 -- . ':(exclude)docs'` 为空 |

即：上述三个 tip 之间只有 `docs/**` 增量，**代码面字节相同**，故车道读数在它们之间可传递；
参与 `docs/**` 的两条车道（⑤ 生成物守卫、⑧ 覆盖闸门）已在最终 tip `09d91216` 由 Gate-0 实跑覆盖（见 §1）。

原始日志：`D:\w21-work\evidence\heavy-lane.log`（重车道）、
`D:\w21-work\evidence\heavy-lane-02-pytest-rerun2.log`（② 复跑）、
`docs/gate/09d91216297bd240169791a450539bc677417365.json`（Gate-0 读数）。

## 1. 车道读数

| # | 车道 | 命令 | rc | 读数 | 结论 |
| --- | --- | --- | --- | --- | --- |
| ① | ruff | `.venv/Scripts/ruff.exe check .` | 0 | 无输出 | PASS |
| ② | pytest-full | `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q -p no:randomly` | **0** | **6723 passed / 26 skipped / 51 deselected / 0 failed，1194.67s** | PASS（见 §2.1） |
| ③ | pytest-clean | `PYTHONUTF8=1 ./scripts/run_tests_clean.sh`（默认目标 `tests/`） | **0** | **6723 passed / 26 skipped / 51 deselected / 0 failed，1141.66s** | PASS（与 ② 逐项相同） |
| ④ | tsc | `node node_modules/typescript/bin/tsc -b`（`web/`） | 0 | 无输出，15.4s | PASS |
| ⑤ | vitest | `node node_modules/vitest/vitest.mjs run`（`web/`） | 1 | **1 failed / 1496 passed**（101 文件：1 failed / 100 passed），77.7s | 唯一红 = 在册 flake（见 §2.2） |
| ⑥ | oxlint | `node node_modules/oxlint/bin/oxlint`（`web/`） | 0 | 0 error | PASS |
| ⑦ | guards | `pytest` 生成物同步 + 验证映射（Gate-0 车道 5） | 0 | PASS，18.04s | PASS |
| ⑧ | coverage | `.venv/Scripts/python.exe scripts/check_review_coverage.py` | 0 | exit 0，8.4s | PASS |
| ⑨ | diff-check | `git diff --check` | 0 | 无输出 | PASS |
| ⑩ | vite build | `node node_modules/vite/bin/vite.js build`（`web/`） | 0 | 构建成功，11.8s | PASS |
| ⑪ | e2e | `node node_modules/@playwright/test/cli.js test --workers=2`（`web/`） | 1 | **496 passed / 10 failed**，12.7m | 10 红全在 `u-project-task.spec.ts`（见 §2.3） |
| ⑫ | 真机验收 | W-16 烟测 + Run A/Run B | — | 见证据件 `48`/`49`/`50`/`51`/`52` | 见 §3 |
| ⑬ | Gate-0 | `.venv/Scripts/python.exe scripts/gate0.py` | **0** | **6/6 PASS，墙钟 40.8s**，读数 `docs/gate/09d91216….json` | PASS |
| ⑮ | Live Gate | Run A ×2 + Run B ×2（真模型 + 生产工具 + 生产账本） | — | 四次 `overall=pass`，见证据件 `49` | PASS |

首轮重车道（`1cfc7b02`）中四条车道 rc 非 0，逐条归因后：② 与 ⑬ 是**环境/流程**问题，
处置后复跑转绿（§2.1 / §2.4）；⑤ 与 ⑪ 是**既有**红（在册环境 flake / 上游已根修的契约漂移），
非本批引入（§2.2 / §2.3）。

## 2. 三条非绿车道的归因

### 2.1 ② pytest-full：仓库根 pytest 走进打包产物树 ⇒ 解释器访问违例

**首轮读数**：`rc=3221225477`（`0xC0000005` 访问违例），墙钟 18.3s，**0 个用例执行**：

```text
Windows fatal exception: access violation
  File "...\desktop\dist-installer\win-unpacked\resources\python\Lib\site-packages\
        win32comext\taskscheduler\test\test_addtask.py", line 23 in <module>
  ... pytest_collection ...
```

**根因（两条，互相独立）**：

1. **收集面**：`pyproject.toml` 的 `[tool.pytest.ini_options]` 只有 `pythonpath = ["."]` 与
   `addopts = ["-m", "not integration and not qiniu"]`，**没有 `testpaths` / `norecursedirs`**。
   verification.md §2 车道 ② 的命令是**仓库根**级 `pytest`，于是它走进两棵 gitignored 的
   electron-builder 产物树——`desktop/dist-installer/`（942 MB，含 `win-unpacked/resources/python`）
   与 `desktop/installer/staging/`——**每棵各含 5402 个 `.py`**（一份完整 Python 3.13 运行时 +
   pywin32）。收集到 `win32comext/taskscheduler/test/test_addtask.py` 时，该模块的模块级 COM 调用
   直接打崩解释器。
   - CI **不受影响**：`gate1.yml` 跑的是 `uv run pytest tests`（显式路径）；
   - 车道 ③（`run_tests_clean.sh` 默认 `tests/`）与 ⑧（`check_review_coverage.py` 自己带路径）也不受影响；
   - **车道 ② 是唯一走仓库根的 pytest**，所以这个坑只在本地跑重车道时暴露。
   - 关键危害：崩的是**解释器**而不是用例，rc 是访问违例、输出里**一个失败签名都没有**，极易被误判成
     "偶发环境抖动"或"跑过了"——实际是**零用例执行**。
2. **环境面**：本地 `.venv` 缺 `memory` extra。把产物树移出收集面后，收集期报
   `ModuleNotFoundError: No module named 'langgraph'`（`tests/memory/test_langmem_store_actions.py:24`，
   `rc=2`）。`pyproject.toml` 的 `memory = [langgraph>=0.6.0, langmem>=0.0.30,<0.1, pymilvus>=3.0.2,<3.1]`
   属 optional extra；CI 的供给口径是 `uv sync --locked --all-extras`。
   本项**已是项目在册环境注记**（`docs/SDD_TICKET_TRACKER.md` L255 同形态记录）。

**处置（不删任何文件、不动依赖声明）**：

- 把两棵产物树**移出仓库**（`D:\w21-work\hold\`，同卷 rename，秒级）→ 跑**文档原样命令** → 移回；
- `.venv` 按 CI 口径补齐：`uv sync --locked --all-extras`（环境操作，`uv 0.10.9` 与 CI 同版本）；
- 复跑前/后逐字节核对冻结安装件：sha256 `01477e59…`、204,379,033 B，**前后相同**。

**复跑读数（本文件 §1 ②）**：`6723 passed / 26 skipped / 51 deselected / 0 failed，1194.67s`，
`rc=0`。51 deselected = `integration` / `qiniu` 标记（与 `addopts` 一致）。

**建议（不在本票施工）**：给 `[tool.pytest.ini_options]` 补 `testpaths = ["tests"]`
（或 `norecursedirs`），让"仓库根 pytest"这条文档命令在**建过安装包的机器**上也不会走进产物树；
同时在 verification.md §2 车道 ② 补一句 `.venv` 须为 `--all-extras` 口径。二者都是**测试配置/文档**面，
不触产品行为。**已登记 #856**（含复现、反证与建议方向）。

### 2.2 ⑤ vitest：唯一红 = 在册环境 flake（负载型超时）

- 全量车道（101 worker 并发）：`1 failed / 1496 passed`，唯一红
  `src/components/StepDetail.window.test.tsx > F5 尾窗… > DIFFS / ARTIFACTS…`，
  `Error: Test timed out in 5000ms`（`StepDetail.window.test.tsx:132`）。
- 隔离单跑同一文件（`node node_modules/vitest/vitest.mjs run src/components/StepDetail.window.test.tsx`）：
  **6 passed / 6**，其中那条失败用例本身 **1301 ms**（文件总 1994 ms）。
- 该用例是**在册 flake**：`docs/PHASE_STATUS.md`「在册环境 flake `StepDetail.window:132` 维持登记」、
  `docs/FRONTEND_ISSUES_LOG.md` 与 B-29 已知 flake 表均在同签名下登记，历史口径为「负载 flake，隔离 6/6 绿」。
- 结论：**非本批引入、非产品缺陷**，与在册签名逐字一致；本机 101 worker 并发是触发条件。

### 2.3 ⑪ e2e：10 红全在 `u-project-task.spec.ts`，spec 锁的是被 #367 取代的旧契约

**读数**：`496 passed / 10 failed`（12.7m）。10 红 = **5 条用例 × 2 个 project**
（`chromium-1280` / `chromium-1920`），全部落在 `web/e2e/u-project-task.spec.ts`，且**全部**失败在同一个断言：

```text
Locator: locator('.project-dialog[aria-label="在此项目中新建任务"]')
Expected: visible   Timeout: 5000ms   Error: element(s) not found
```

隔离复现（`--workers=1 --project=chromium-1280`）：同样 **5 failed**，即确定性红，不是并发抖动。

**根因链（读代码 + 失败快照，非猜测）**：

1. 失败快照里**确实有一个弹窗**，但它是 `- dialog "新建任务"`，内含 `heading "新建任务"`、
   `textbox "任务描述"`、`textbox "完成目标 （可选）"`、`radiogroup "自主度"`、按钮
   `取消` / `创建任务`。
2. 对应实现是 `web/src/components/TaskCreationDialog.tsx:161`：
   `<Dialog.Content className="project-dialog" aria-label="新建任务">`。
3. spec 期待的是 `.project-dialog[aria-label="在此项目中新建任务"]` + `.project-path-callout` +
   「没有『任务内容』输入框」+ 弹窗内权限档 picker + `launch=false`——即 **#204 裁定的收窄形态**。
4. 该形态已被 **#367 [W-23] 选项 A**（`da15848c`，用户 2026-10-06 批准）取代：旧组件
   `StartTaskInProjectDialog` 在 `805ff39d` 删除，换成 prompt 优先、创建即启动的
   `TaskCreationDialog`。**spec 未随该票重写** ⇒ 选择器永久失配 ⇒ 该文件 5/5 恒红。

**这一红已经在上游被根修**：main 的 **PR #819 `codebuddy/fix-preexisting-failures`**
（commit `b895940b`，merge `1ba082cd`，提交信息即「u-project-task 重写为 #367 选项 A 契约
（**10 个 pre-existing 失败根修**）」，自述两 project 各 5/5、合跑 10/10 过）。
`git merge-base --is-ancestor b895940b HEAD` = **NO**；本分支基点 `cbf08285`（2026-10-07 16:54 +0800）
早于该修复进入 main，故本分支没有它。
（同族还有一笔无效修复 `cbb24a2d`「弹窗断言加 10s 超时」——把永久性契约变更当超时治，
其提交信息在 `b895940b` 里被明确点名。）

**结论**：**非 W-21 引入**；是分支基点落后 main 造成的**在册既有红**，同步 main 即消除。

### 2.4 ⑬ Gate-0 首轮 rc=1：工作树有未提交改动，读数落盘被拒

首轮（`1cfc7b02` + 未提交的 N-3 文档改动）6/6 车道 **PASS**，但 `gate0.py` 依协议 §8.7 第 3 条
**拒绝落盘**——工作树偏离 HEAD 会让读数指向"一棵没被测过的树"。处置：把 N-3 改动落成
`09d91216`（docs-only）后重跑，**6/6 PASS，读数已落盘**
`docs/gate/09d91216297bd240169791a450539bc677417365.json`。

## 3. ⑫/⑮ 与 #365 票面的对应

- **⑫ 真机验收**：W-16 烟测各腿见证据件 `48`（安装/冷启动/开窗）、`51`（旧会话与模型配置可见、
  卸载器双语、磁盘满回退仅逻辑）、`52`（逐项裁决表）。
- **⑮ Live Gate**：Run A ×2 + Run B ×2 全部在**冻结安装件**（sha256 `01477e59…`）上以真模型跑完，
  四次判定 `overall=pass`，见证据件 `49`。桌面/TUI 一致性与不一致实测见证据件 `50`。

## 4. 诚实注记

1. **本分支落后 main 64 个提交**（`git rev-list --count HEAD..main` = 64；本地 main `9e1c065b`，
   `origin/main` `5b7c14c3` 又比本地 main 多 58 笔）。冻结安装件 `28a382cc` 因此**不对应任何将被合并的树**。
   按 §14.10「树不同重跑完整门禁」，集成前须先同步 main 并在合并树上**重跑全量门禁**；
   同步会带入产品面改动（含 #367 的 UI 变更），故 Run A/B 的 Live 证据是否可传递须按 §8.8 规则**重新判定**，
   不能默认沿用。
2. 本轮 ② 的绿读数是在**产物树被移出收集面**的条件下取得的（即复现了 CI 的树形态）；
   命令本身逐字未改。**未**修改 `pyproject.toml`、**未**删除任何产物文件。
3. 车道 ③ 与 ② 同套；其存在理由是绕开 WorkBuddy 沙箱经 `PYTHONPATH` 注入的 `sitecustomize.py`
   删除配额 shim。本机 `PYTHONPATH` 未设置、`import sitecustomize` 为 `ModuleNotFoundError`
   ⇒ 该 shim **不在场**；仍实跑一遍以取读数：**6723 passed / 26 skipped / 51 deselected，1141.66s，rc=0**，
   与 ② 逐项相同（`run_tests_clean.sh` 默认目标 `tests/`，故也不受 §2.1 的产物树问题影响）。
4. ⑪ 的 10 红**全部**在同一 spec、同一断言，已逐条核对失败清单与失败快照；未发现其他 spec 红。
5. 本文件不宣称「全量门禁在冻结树上全绿」：车道 ⑪ 在冻结树上确实红（原因见 §2.3），
   该红的消除依赖同步 main，属待批准动作。
