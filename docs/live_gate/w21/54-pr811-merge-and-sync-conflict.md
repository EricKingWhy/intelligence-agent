# 54 — PR #811 集成完成 + main 同步冲突报告（2026-10-08）

本件记录两件事：① PR #811 的集成收口（**已完成**）；② 把 `origin/main` 同步进
`fix/w21-windows-gate-fixes` 的**只读预演**结果与 §14.7 九项报告（**待批准**，未动工作树）。

---

## 1. PR #811 已合并（`docs(#365): W-21 Windows 实测 = fail + 6 个缺陷根因`）

**head**：`df6d219a`（docs-only，3 文件）

| 文件 | 变更 |
| --- | --- |
| `docs/review_ledger.d/t365-pr811-sync-merge-ac02cc35.tsv` | 新增 1 行（本件的机械归属行） |
| `docs/SDD_TICKET_TRACKER.md` | +1 行 |
| `docs/live_gate/w21/40-windows-gate-result.md` | +192 行 |

**首次合并阻塞的根因**：`strict: true` 要求分支与 main 同步，而 rebase 属默认禁止项 ⇒ 只能
merge；GitHub 的 `update-branch` 造出合并 `ac02cc35`，其 `git show --cc --name-only` **为空表**
且合并树 `8ffdf898` **既不等于父一树** `105022e7` **也不等于父二树** `f67b53ab` ⇒ 正是
`scripts/check_review_coverage.py` 的 `zero_content_parent()` docstring 明文登记的
**「已知残余」（两侧各改不同文件取并集）**，该形状**刻意 fail-closed**。

**处置**（按该 docstring 与 `t806-sync-merge-758f2735.tsv` 先例）：补一条**机械归属行**，
range `3be2196ed5c603109e5ec3592872ca76a40f60ec..ac02cc35c2338e3a778334d4b70f2016a7bdddeb`
（`git rev-list ac02cc35 --not 3be2196e` = 恰该 merge 自身）。行内明写「只做机械归属，不是对
内容的审查主张」，并逐条列出两侧文件面：

- 相对父一（本线 tip `3be2196e`）= main 侧 PR #855 面 **5 文件**：3 条 `t12o-*` 台账行 +
  `web/e2e/multiturn-queue.spec.ts` + `web/src/hooks/useSession.ts`（**均已由 main 侧既有归属覆盖**）；
- 相对父二（`a223cca4`）= 本线 **docs-only 2 文件**。

**机械读数**：

| 检查 | 读数 |
| --- | --- |
| 落行前 coverage | **exit 1**，唯一 ❌ = `ac02cc35` |
| 落行后 coverage（含行提交 `df6d219a` 自身） | **exit 0**，`覆盖区间 09ca47a1..HEAD` / 提交总数 3061 / 已审查 2998 / 待判定 63（63 项全为历史既有归属，非本批） |
| 行提交是否被自动放行 | `✅ 台账自身更新（自动放行）: df6d219a` |
| 行 blob sha256 | `88511872cec6623ee2e5d73248b13682f8fec9e1ad347f9237c3deed1669cfea`（与写盘脚本断言一致） |
| 行整行长 | 754 字符（lint 上限 800） |
| CI `gate0` | **pass**（run `37740100572`，20s 内转绿） |
| CI `gitleaks` | pass |
| merge commit | **`77ac2cda`**，`mergedAt 2026-10-08T06:54:13Z`，`origin/main` 现 tip |

**遗留登记**：`77ac2cda` 是 main 顶端**尚未记账**的 merge。`gate0.yml` 只跑 `pull_request`
不跑 `push` ⇒ 它不会被自己的 PR 检查；但**任何后续从 main 开出的 PR**，其覆盖区间（左端 =
台账里最早且可达 HEAD 的 base）会包含它 ⇒ 需要一行归属。预期形状：该 merge 的 `--cc` 非空
（两侧并集）⇒ 不能走 docs-only 自动归属，需真实审查行或机械行。**本票的 PR 会撞上这一条**，
届时一并处置。

---

## 2. main 同步进 `fix/w21-windows-gate-fixes`：只读预演

**方法**：`git merge-tree --write-tree --name-only HEAD origin/main` —— 只读预演，
**未动工作树、未改任何冲突文件、未 add**。

| 项 | 值 |
| --- | --- |
| 本线 HEAD | `954daeb9` |
| `origin/main` | `77ac2cda` |
| merge-base | `cbf08285` |
| 落后 main | **135 提交** |
| 预演结果树 | `301eaf55effddccf41ba281a63f53a24f2969280` |
| 冲突 | **2 处，均在进度文档** |
| 代码文件 | **3 个自动合并、零冲突** |

### 2.1 冲突形状（机械读数）

| 文件 | ours vs base | theirs vs base | 冲突区数 | 冲突区内 ours / theirs 行数 |
| --- | --- | --- | --- | --- |
| `docs/SDD_TICKET_TRACKER.md` | +160 / −0（1 hunk，base 行 7579 EOF） | +30 / −0（5 hunk：行 3、17、55、7281、7579） | **1** | **160 / 10** |
| `docs/phase_status/2026-10.md` | +112 / −0（1 hunk，base 行 942 EOF） | +22 / −0（2 hunk：行 3、942） | **1** | **112 / 15** |

**关键事实：两侧都是纯插入（`--numstat` 的删除列全为 0）**，且冲突区都落在 base 的 **EOF 锚点**。
并集 = ours 全部 + theirs 全部（机械核对：7769 = 7739 + 30；1076 = 1054 + 22），**无任何行丢失**。

### 2.2 三个代码文件的自动合并语义核对

| 文件 | ours vs base | theirs vs base | 自动合并是否两全 |
| --- | --- | --- | --- |
| `src/agent_harness/config.py` | +4 / −0（`web_dist_dir`，W-21 D3） | +27 / −1（#822 附件上限 6 字段 + 构造期 validator + import） | ✅ `web_dist_dir`=1、`attachment_max_image_bytes`=1、`_validate_allowed_media_types`=1、`field_validator`=2 |
| `src/agent_harness/web/app.py` | +37 / −6（`mount_static` 候选序 + `Settings.web_dist_dir` 接线，W-21 D3/D5） | +17 / −5（#822 附件 router + `BodyDepthGuardMiddleware(exempt_path_pattern=…)` + EXPOSED 注释收束） | ✅ `_bundled_web_dist`=2、`web_dist_dir`=5、`register_attachment_routes`=2、`ATTACHMENT_UPLOAD_PATH_RE`=3、`import sys`=1、`EXPOSED_CUSTOM_RESPONSE_HEADERS`=2 |
| `tui/src/app.ts` | +9 / −2（`authorizedFetch` 凭据通道，W-21 D5） | +46 / −2（#382 进度清单横幅 + `ctrl+t` + 去 `renderIncremental` 早退） | ✅ `authorizedFetch`=3、`fetchImpl`=4、`renderPlanList`=2、`planExpanded`=4、`matchesKey`=4 |

**逐 hunk 核对结论**：三文件的 hunk 行号区间**两两不相交**（例：`app.ts` ours 改 base 行 29/46/70/91/178，
theirs 改 base 行 12/40/78/118/336/349/363/384/407），故 `ort` 自动合并成立、无需人工介入。

**机械验证**：合并树内三文件**零冲突标记**（`grep -lE '^(<<<<<<<|>>>>>>>)'` = 0 文件）；
合并后的 `config.py` / `app.py` 经 `ast.parse` 通过（10367 / 141664 字符）。
合并树 `src` 子树 `8ae32a2c` ≠ ours `e47efe9b` ≠ theirs `9b2aad21` ⇒ 是**真并集**，不是任一侧。

---

## 3. §14.7 九项报告（`docs/agents/git-workflow.md` §4）

**① main 改了什么、目的**
两个进度文档各追加了新章节：`TRACKER` = `## #808 审查登记发现全数修复（2026-10-07）`（EOF 10 行）
另加中部 `claude/663-remainder-r2` 段（base 行 7281 起 13 行）与顶部 3 处索引行；
`phase_status` = `## 2026-10-08 · #832 live_gate 缺少 Docker CLI…`（EOF 15 行）与顶部
`## 2026-10-08 · Matt skills 1.3.1 兼容更新发布`（7 行）。目的：把 #808 / #832 / 663 残余 /
Matt skills 1.3.1 的施工与门禁事实落进进度文档。

**② 本线改了什么、目的**
两个进度文档各追加一个 W-21 章节：`TRACKER` = `## W-21 Windows 发布链修复批（2026-10-07；
#809 → #812–#817）`（160 行）；`phase_status` = `## 2026-10-07 · W-21 Windows 发布链六缺陷修复批`
（112 行）。目的：把 #812–#817 六条修复、D5 产物证据、重打包、W-16 烟测、Run A/Run B、
B-4 缺陷、13 条车道读数与三条红归因落进进度文档。

**③ 冲突原因**
两侧都在 base 的 **EOF 锚点追加新章节**。git 的 3-way 合并对「同一锚点各插一段」无法定序 ⇒
标记冲突。**不是语义冲突**：两侧内容互不引用、互不修改，且**两侧删除行数均为 0**。

**④ 两侧逻辑能否同时保留**
**能，且应当**。并集 = ours 全部 + theirs 全部，逐字节无丢失（§2.1 的计数闭合）。这正是
本仓「并集零删除解决」的既有做法（先例：`#781` 的 `PHASE_STATUS.md` 冲突、`#248` 的 merge）。

**⑤ 推荐最终语义**
- `TRACKER`：EOF 处先接 main 的 `## #808 …` 段（10 行），再接本线 `## W-21 …` 段（160 行）；
- `phase_status`：EOF 处先接本线 `## 2026-10-07 · W-21 …` 段（112 行），再接 main 的
  `## 2026-10-08 · #832 …` 段（15 行）——两侧其余 hunk（顶部索引行 / 中部段落）保持 git 自动合并结果不动。
- 理由：两文件章节序为**按日期升序**（`phase_status` 实测：行 951 `2026-10-07 · #808` 在
  行 958 `2026-10-08 · #832` 之前）；`TRACKER` 两段同为 2026-10-07，取「来侧在前、本线在后」
  以让本线章节留在 EOF（与本线既有追加位置一致）。
- 机械核对项（解决后立即执行）：冲突标记 0 残留；两侧段头各 1 在场；`git diff --check` clean；
  并集行数 = 7769 / 1076。

**⑥ Contract 影响**：**无**。两文件都是进度记录，不参与任何代码契约、schema 或 API 面。

**⑦ Runtime Behavior 影响**：**无**。两文件不进构建产物、不被运行时读取。
（代码面的三文件自动合并**不改任何既有行为**：ours 是新增可选配置/候选目录/凭据注入，
theirs 是新增附件路由与进度横幅，两两互不遮蔽——见 §2.2。）

**⑧ Test 影响**：**无测试文件落在冲突面**。但按 §14.10「树不同重跑完整门禁」，
合并树 ≠ 已验证树 `28a382cc` ⇒ **必须在合并树重跑全量 13 条车道**，并重判 Run A/B 的
Live 证据能否传递（§8.8）。预期新增红面：main 侧带来的 `#822` 附件测试、`#382` TUI 测试
需要本机 `.venv` 与 `tui` 依赖在位（当前 `.venv` 已按 CI 口径 `uv sync --locked --all-extras` 补齐）。

**⑨ 风险等级**：**低**。
- 冲突面 = 2 个 docs 文件、纯插入、零删除、并集无损；
- 代码面 3 文件自动合并、hunk 不相交、双全验证通过、`ast.parse` 通过；
- 反向代价 = 若语义裁决有误，只需重做这一次 merge（`git merge --abort` / 重解），
  **不推、不泄漏、不可逆面为零**。

---

## 4. 待批准

按 §14.4「冲突文件修改与解决后的 add」属**每次单独批准**项，按 §14.7「获批前不修改冲突文件、
不 add」——**本次到此停止**，工作树仍 clean、未动任何冲突文件。

**请求批准**：按 §3⑤ 的推荐语义做**并集零删除**解决，然后按下列顺序收尾：

1. 解决 2 处冲突（并集零删除）+ 机械核对（标记 0 / 段头各 1 / `diff --check` clean / 行数闭合）；
2. 提交 merge；
3. 覆盖闸门：本 merge 的 `--cc` 预期 = 5 文件（2 docs + 3 代码）⇒ **不能** docs-only 自动归属，
   需补归属行（先落行、跑 `check_review_coverage.py` 到 exit 0）；
4. §14.10 在合并树**重跑全量 13 条车道**（含 lane ⑪ e2e 应随 main 的 `b895940b` 转绿）；
5. §8.8 重判 Run A/B 的 Live 证据可否传递（main 带入产品面改动 ⇒ 很可能需重打包 + 重跑）；
6. push `fix/w21-windows-gate-fixes` + 开 PR（§14.4 单独批准项）。
