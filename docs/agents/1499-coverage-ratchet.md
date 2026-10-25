# #1499 实现说明：覆盖率基线留痕与变更行棘轮（A 案）

> 状态：**已实现**（代码面 tip 见 PR；本文件随代码一起交付）。
> 依据：Issue #1499 全文（含已裁结论 11 = A 案）与票面「卡内事实与边界摘录」。
> 遵循：`AGENTS.md` §6.1 方案先行调研 + §9.5 懒惰阶梯 + `docs/SDD_WORKFLOW_PROTOCOL.md` §1.3。

## 1. 起点事实（本票开工时实读，非引用）

- `pyproject.toml` **无任何 `[tool.coverage.*]`**，也没有 `fail_under`（`grep -n -i coverage` 只命中
  dev 依赖行 `pytest-cov>=7.1.0`）。
- `.github/workflows/gate1.yml:66–73` 与 `gate-nightly.yml:63–70`：`uv run pytest tests
  --junitxml=… --cov=src/agent_harness --cov-report=term --cov-report=xml:artifacts/coverage.xml`，
  **命令里没有任何阈值**；报告只经 `upload-artifact` 上传。**本票不改这两条命令**。
- `gate1` 只在 `push: main` / 手动触发（文件头 `:263` 附近的说明与 `on:` 段一致）⇒ **PR 侧没有
  行覆盖率读数**；`gate0` 的 `coverage` 车道是**审查台账覆盖闸门**（`check_review_coverage.py`），
  与行覆盖率无关，两者不可混算。

## 2. 方案依据（协议 §1.3；独立来源 ≥2，一手正文）

| 来源 | 一手正文（本轮抓取核对） | 机制 | 契合点 / 判定 |
| --- | --- | --- | --- |
| **Lighthouse CI** `docs/configuration.md`（`GoogleChrome/lighthouse-ci` main） | `off` / `warn` / `error` 三级；`warn` 原文 *"the audit result will be checked, and the result will be printed to stderr, but failure will not result in a non-zero exit code"*；`error` 才 *"failure will result in a non-zero exit code"*；未列出的审计默认 `off` | 判定分**可观测**与**阻断**两档，门槛可按审计项逐条配置 | **ADAPT**：全仓总率走 `warn` 语义（打印 + 入基线，不判定），只有**变更行**这一条走 `error` 语义（< 60% 非零退出）。不引入新框架 |
| **pa11y-ci** `README.md`（`pa11y/pa11y-ci` main） | `--threshold <number>` 原文 *"permit this number of errors, warnings, or notices, otherwise fail with exit code 2"*；且 *"You can also set `threshold` in your configuration's `defaults` or individual URL options. These thresholds apply per page"* | 门限是**分范围**的、可谈判的，不是全仓一个硬数 | **ADAPT**：判定面按"变更行"切分；盲区按**精确文件**登记（见 §4），不做"整域排除" |
| **diff-cover** `README.rst` + `diff_cover_tool.py`（main） | `--fail-under` 原文 *"To have `diff-cover` and `diff-quality` return a non zero status code if the report quality/coverage percentage is below a certain threshold"*；实现 `if percent_covered >= fail_under … return 0`（**浮点** `percent_covered`，`>=` 语义）；`--exclude` 走 `fnmatch`；`--expand-coverage-report` 一节自陈 *"diff reports list all lines that changed, coverage reports usually list code statements. As a result, a change in a multi-line statement may not be analyzed"* | 它就是"变更行覆盖率"的原产工具 | **不采用（BUILD 侧保留）**：① 未在本仓依赖里（`pyproject.toml` / `uv.lock` 均无），引入 = 新增依赖，与 §9.5 第 5 级相悖；② 判定走浮点百分比 ⇒ 正是 B 案要绕开的那类精度面，本票要的是整数 `covered*100 >= total*60`；③ 它**不**提供"报告是否属于这棵树"的对账（sha256 + tree），而那正是 AC1 的核心；④ 白名单要"精确文件 + 理由 + 平台"的解析期护栏（AC3），`--exclude` 的 `fnmatch` 反而允许通配 |
| **coverage.py** `config.html` `[report] fail_under`（B 案对照） | *"A target coverage percentage. If the total coverage measurement is under this value, then exit with a status code of 2."*；*"If you specify a non-integral value, you must also set `[report] precision` properly to make use of the decimal places."*；*"A setting of 100 will fail any value under 100, regardless of the number of decimal places of precision."* | 全仓单一硬阈值，**对精度/取整敏感** | **不采用**（DECIDE：B 案 = 票面否决项）。A 案用整数比较 `covered*100 >= total*60` 绕开取整面；且全仓总率只记录 |

> 前三条是**机制旁证**，第四条是**反面参照**（B 案被否的理由）；四条都不是"行业强制值"：
> 60% 这个数字来自票面裁决允许设定的低门槛，本票不冒称它是实测值或行业标准。

## 3. 设计（落点与职责边界）

| 落点 | 职责 | 为什么在这里 |
| --- | --- | --- |
| `scripts/check_line_coverage_ratchet.py` | 两个子命令 `baseline` / `ratchet`；读 coverage.py 已产出的 Cobertura XML | 复用 CI **已经在产**的那份报告，不新增测量工具、不引入新依赖（§9.5 第 2/5 级） |
| `.github/workflows/coverage-ratchet.yml` | PR 侧重车道：跑与 gate1 逐字同形的 pytest+覆盖率 → `baseline` → `ratchet --base origin/main` | 棘轮需要真报告 ⇒ 分钟级 ⇒ **不能进 gate0**（≤60s 机械车道、且是必需状态检查） |
| `.github/workflows/gate1.yml` / `gate-nightly.yml` | 各**追加一个 `baseline` 步骤**（覆盖率命令逐字未改） | 「基线留痕」的载体；`main` 每次推进、每晚各留一条可复查记录 |
| `docs/agents/coverage-ratchet.whitelist.json` | 盲区白名单（精确路径 + reason + platform） | 与 `verification.map.tsv` 同族：都是**流程输入**，不是产品代码 |

**判定口径**（全部机械）：

1. 分母 = `git diff -U0 --no-renames <base>...HEAD` 里 `src/` 下 **`.py`** 的**新增行号** ∩
   报告里该文件的**可执行行**；分子 = 其中报告命中数 > 0 的行。非 `.py`（快照 / 数据文件）
   没有可执行行，既不进分母、也不该把判定打成不可判。
2. `covered * 100 >= total * 60` ⇒ 通过（**恰好 60% 通过**，`>` 会把它判失败）。
3. 白名单命中文件的变更行**不进分母**；白名单解析期拒绝通配 / 目录前缀 / 非规范化路径 / 空字段。
   **白名单判定先于「未测量」判定** —— 它要救的正是"报告里根本没有这个文件"的盲区（容器内代码、
   平台专有实现）；顺序颠倒会让这份白名单永远救不了它该救的文件。
4. `--move-only-note <说明>` **且** `git -M` 判为 **`R100`（逐字相同的纯移动）** 才跳过；
   `R<100`（移动 + 修改）与缺说明都照常计入 —— `-M` 的默认相似度阈值是 50%，那是"改过"不是"纯移动"。
5. 全部不可判情形（缺报告、缺基线、报告 sha256 与基线不符、基线不是当前树、`git diff` 读不出
   ——含两侧无共同祖先、改了 `src/` 但报告里没有该文件、`--base` 解不出、白名单不合法）
   ⇒ **退出 2**，绝不退化成绿。git 读失败被当成"没有变更"会把一次读失败洗成 `RATCHET EMPTY`。
6. 分母为空 ⇒ 打印 `RATCHET EMPTY` 并退出 0（理由见下）。

### 3.1 零可执行变更为什么选"显式通过"

两个候选：① 失败；② 通过 + 显式打印。**选 ②**：

- 判"失败"会把 docs-only / 纯注释 / 纯移动 PR 无故变红 —— 那正是 A 案要消灭的等待代价（AC3 末尾
  要求"正常单人 PR 等待代价留证"）。
- 判"不可判（2）"同样阻断正常 PR，且语义不准：**"没有变更的可执行行"是一个确定的结论**，
  不是"判不了"。
- 静默通过（只打印 PASS 不说明）会让"这次一条可执行行都没测"隐形成立 ⇒ 所以单独一行
  `RATCHET EMPTY` 说明空集合语义，读的人不会把它误读成"覆盖了 100%"。

### 3.2 树的判据是 `^{tree}`，不是 SHA

基线记 `sha` + `tree`；`ratchet` 只比 **tree**。理由与协议 §8.1 第 3 条同源：内容相同的树就是
被测量过的那棵树，空提交 / 只追加 docs 的提交不该让读数失效。反过来，**树不同 ⇒ 不可判**：
`README` 改一行也是另一棵树，那时这份报告不再对应当前代码。

## 4. 盲区白名单：为什么初始为空

`docs/agents/coverage-ratchet.whitelist.json` 的 `entries` 初始为 **`[]`**。理由：

- 加条目要能指出**测不到**的机器事实（该文件的可执行行在本平台到不了 / 追踪器看不见该进程），
  而不是"这个文件覆盖率低"。前者是盲区，后者是欠账 —— 本票明确不追补历史欠账。
- 候选形状写在文件的 `candidate_blind_spots` 里（平台专有实现 / 容器内代码 / 平台 API 绑定），
  按需**逐条**登记，且必须写明 `platform`。
- 空表是**最严**的初始态：它不会把任何文件悄悄放过闸，也把"该不该加"变成一次显式提交。

## 5. 与既有门禁的关系（不新增第二套真相）

- **不改** `docs/agents/verification.map.tsv` 的车道语义：棘轮不是 Gate-0 车道。本票只在
  `scripts/` 行的 `focused` 里追加 `tests/tooling/test_line_coverage_ratchet.py`
  （让"改了闸门脚本 ⇒ 跑它的守卫"在 `--affected` 路径上成立）。
- **不改** gate1 / gate-nightly 的覆盖率命令。加的是 `baseline` 一步（只记录）。
- 棘轮**不是**必需状态检查（与 `e2e-smoke` 同一条教训：带 `paths` 过滤的检查若挂成必需，
  不碰 `src/**` 的 PR 会永久停在 "Expected — waiting for status to be reported"）。
  要把它变成硬门须先改分支保护 —— **需用户授权，不在本票范围**。

## 6. 已知边界（如实登记）

1. **容器 / 子进程内的代码在本报告里本就看不见**：`--cov` 只追踪宿主进程树（`code_worker_runner.py`
   是在 Docker 计算容器里单独执行的）。这类文件被改动时，`ratchet` 判**不可判（2）**而不是静默算
   0%；要放行必须**显式登记进白名单**（白名单判定先于"未测量"判定 —— 否则它救不了它该救的文件）。
   白名单命中但报告里没有该文件时，输出会标注"报告里没有该文件（盲区）"，不让豁免隐形。
2. **`git -M` 的 rename 判定有 50% 相似度阈值**：`R<100` 一律按新增计（只有 `R100` 才算纯移动）。
   这是刻意保守的方向（宁可多算分母，不可少算）。
3. **只覆盖 `src/**.py` 的新增行**：`web/**`（TS）、`tui/**`、`scripts/**` 的变更行不在本棘轮分母内
   —— 它们各有多测/类型检查车道，且 coverage.py 不覆盖 TS。`src/` 下的非 `.py`（快照/数据）
   没有可执行行，同样不进分母。把这条写进 PR 描述与本文，避免"棘轮绿 = 全仓都测了"的误读。
4. **本工作流的等待代价**：与 gate1 backend 作业同形同命令（`uv sync` + 全量 pytest + 覆盖率），
   单人 PR 每碰一次 `src/**` 就多等这一轮。实测读数见 PR 描述（本文件不复制数字，避免漂移）；
   引用时连同来源与机器负载一起给。
