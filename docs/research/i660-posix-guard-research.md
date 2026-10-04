# i660 · W-05 progress writer POSIX 防护调研报告（Phase 1，仅调研）

> Ticket：issue #660 方向 2（用户已拍板）——给写入器加 POSIX 侧防护，
> 让「外部锁定 / 只读目标 ⇒ 明确失败且旧文件原样」在 Linux 上也成立。
> 状态：调研与推荐方案已交付；未写代码、未写实现计划（含草案）——按任务红线写完本报告即停。
> 诚实声明：本环境下 Bash 任意执行 / pytest / WebFetch / WebSearch / gh 均被宿主权限拒绝
> （尝试记录见 §7），协议 §6.1 要求的「≥2 独立来源 + 本地浅克隆 file:line + commit 实读核实」
> 未能在本会话完成——§4 的来源引用均为模型既有知识（逐条标注置信度），
> 下游施工前必须按 §6.1 补核实。
> 本地可核实的部分（源码 / 测试 / 票面 / 已有锁实现）均已实读并给 file:line。

## 0. 启动检查表（AGENTS.md §3 五项）

| 前置项 | 实际读取依据 | 状态 |
| --- | --- | --- |
| Vision 相关原则 | `SPEC_ROOT/00_PROJECT_VISION.md` §2.1 Lightweight、§2.3 Recoverable、§2.5 Reuse First、§3 冻结原则（L1-122 实读） | READY |
| 当前任务规格 | `docs/tickets/workbench-2026-09-27/W-05-progress-file-writer.md`（W-05 票面 + 既有方案依据块，全文实读）；`docs/PRD_RECOVERABLE_REVIEWABLE_AGENT_WORKBENCH.md` L29/L36/L49/L66-68/L92（进度文件契约）；`src/agent_harness/session/progress.py` 模块 docstring | READY |
| Reuse 相关判定 | `SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md` §1 强制流程 + 判定口径（L1-25 实读） | READY |
| Phase 依据 | `docs/PHASE_STATUS.md` 当前工作焦点 2026-10-04 段（L43 实读）——#660 为 W-05（#349，WorkBuddy W 批）交付后的 gate1 红因登记票，属 WorkBench 产品化线收口，不新开 Phase | READY |
| 本任务触发细则 | `AGENTS.md` §6.1/§6/§8/§9.1.1；`docs/SDD_WORKFLOW_PROTOCOL.md` §1.3（方案依据五字段，L45-60 实读）；`docs/agents/reference-sources.md`（全文实读）；用户任务提示词（优先级 1） | READY（附阻塞说明：外部来源核实被宿主权限阻塞，见文首诚实声明） |

注：`gh issue view 660` 被宿主拒绝，issue 原文未实读；现象/根因/证据以任务提示词 + `docs/PHASE_STATUS.md` L43 焦点段 + `docs/SDD_TICKET_TRACKER.md` L7205 交叉印证（三者一致：gate1 run 37211548325 @ b7aad181，2 failed / 5656 passed，唯一红因即本两用例）。

---

## 1. 问题定界（实读证据）

### 1.1 两用例期望什么（tests/session/test_progress_file.py:387-413）

- `test_locked_target_fails_explicitly_and_keeps_old_file`（L387）：writer 执行期间用存活 r+ 句柄占住目标文件，期望 `outcome.ok == False`、`error_kind` 取 `locked`/`env`，句柄释放后旧文件字节不变。
- `test_readonly_target_fails_explicitly`（L401）：`os.chmod(target, stat.S_IREAD)` 后写，期望明确失败，finally 恢复权限，旧内容原样。

### 1.2 现在的实现为什么在 Linux 上挡不住（src/agent_harness/session/progress.py:596-660）

写入路径：`mkstemp` 同目录临时文件 → flush+fsync →（有旧版则 copy 到 `progress.prev.md`）→ `os.replace(tmp, target)`（progress.py:638-650）。失败分类只靠异常面：`PermissionError → fail("locked")`、其余 `OSError → fail("env")`（progress.py:657-660）。

两个 POSIX 事实（进程内核语义，非本仓实现问题）：

1. **rename 不查目标文件权限与句柄**。POSIX `rename(2)` 只要求对源目录和目标目录有写权限；目标文件自身的 mode 位、以及是否有别的进程开着句柄，都不影响 rename 成功。所以 Linux 上 `os.replace` 照常成功 → 两用例拿到 `ok=True` → 断言红（假绿语义：Windows 上“锁得住”的形态在 Linux 上锁不住）。
2. **`open(target, "r+")` 不是锁**。POSIX 上打开句柄不阻止任何目录操作；没有任何 writer 侧探针能可靠探测“别人正开着这个文件”（`/proc/*/fd` 扫描不可靠且有 TOCTOU，不是可发布的能力）。

结论：方向 2 若要成立，锁必须是**写入器自己定义并执行的跨平台协议**，且测试里“模拟外部锁定”必须改走同一协议（不再用 Windows-only 句柄把戏）；只读检查必须改为**写前显式权限检查**，不能指望 `os.replace` 替我们查。这正是任务提示词点出的技术张力。

### 1.3 关键仓库内发现：跨平台锁协议已有先例（懒惰阶梯第 2 级）

`src/agent_harness/instance_lock.py`（#150 单实例锁，全文实读）已实现并生产使用：

- `_take_os_lock` / `_release_os_lock`（L101-124）：POSIX `fcntl.flock(LOCK_EX|LOCK_NB)` / Windows `msvcrt.locking(LK_NBLCK)` 双平台分支；
- OS 级 advisory lock，进程崩溃/被 kill 由 OS 自动释放，无陈旧锁问题（L9-12）；
- 明确的边界声明：advisory 只约束遵守协议的进程（L14-16）——与本调研推导的「锁协议由写入器拥有、测试走同一协议」结论同构；
- Windows 区间锁偏移远离载荷区的经验（`_WIN_LOCK_OFFSET`，L66-71）。

影响：方向 2 的锁协议**不需要从零发明**——仓库内已有成熟同构实现，新代码只是在 progress 目录复刻其协议形态（不复用 InstanceLock 类本身，理由见 §3.3）。

### 1.4 CI 用户身份（任务要求核实项）

- 本地开发环境为 root（uid=0，实测 `id`）——**root 绕过权限位**，本地验证只读用例时需注意 `os.access` 恒真的假象；本会话本地复跑被宿主拒绝（§7），未能实测。
- CI 身份未实读（gh 被拒）。GitHub 官方文档口径（既有知识，置信度高，待核实）：ubuntu-latest 托管 runner 以非 root 用户 `runner` 执行。若属实，`os.access(target, W_OK)` 前置检查在 CI 有效；施工票必须复述该核实（run 日志 `whoami` 或官方文档链接），并以「root 下检查自动放行不误伤」作为附加验证点。

---

## 2. 方案空间

三个正交子问题：

- A. 锁协议形态（解决“外部锁定 ⇒ 明确失败”）；
- B. 只读前置检查形态（解决“只读目标 ⇒ 明确失败”）；
- C. 测试改造思路（在不变弱测试语义的前提下让两用例表达真实协议）。

### 2.1 A：锁协议候选

| 候选 | 机制 | 跨平台 | 对本问题契合 |
| --- | --- | --- | --- |
| `fcntl.flock` / `msvcrt.locking` 锁文件协议（仓库 `instance_lock.py` 已有） | writer 写前对同目录锁文件取非阻塞 OS 排他锁；持锁者即“写入权”持有者 | ✅（仓内已验证） | 直接命中：测试与 writer 走同一协议后，“外部锁定”= 持有同一把锁，POSIX 可判定 |
| `filelock`（PyPI） | 同 flock/msvcrt 方案的成熟封装，lockfile 单例、超时/轮询、平台分支齐 | ✅ | 机制同上，但为零能力差异的新依赖（协议面与 flock 完全同构） |
| `portalocker` | 同上，另含 `LockFlags` 封装 | ✅ | 同上 |
| O_CREAT\|O_EXCL 原子锁文件 | 存在性即占用 | ✅ | 需自行处理陈旧锁清理（进程被 kill 留残留文件）——正是 instance_lock.py docstring 明确拒绝的形态（L9-12） |
| 强制锁（mandatory locking / lease） | 内核级 | Linux 特有/默认关闭；macOS 无；Windows 语义不同 | 不可移植，排除 |
| 检测外部句柄（/proc 扫描） | — | — | 不可靠、TOCTOU，不具备协议资格（§1.2 结论） |

（以上库均为既有知识，置信度高，未按 §6.1 完成 file:line 实读核实——见文首声明。）

### 2.2 B：只读前置检查形态

POSIX 上要拦住“对只读目标的覆写”，只能由 writer 主动检查，候选：

1. **`os.access(target, os.W_OK)`（或检查 `stat` 写位）**：目标存在且不可写 → `fail("locked"/"readonly", ...)`。简单、无副作用；root 下恒真（放行），CI 非 root 下生效（§1.4）。注意 Windows 语义不同：Windows 只读位会真让 `os.replace` 抛 `PermissionError`，现状已工作；`os.access` 在 Windows 上对只读位返回 False（也拦截），行为一致。
2. **先试写探测**（append 模式 open 探针）：有副作用风险（改变 mtime/截断风险），且对“只读位”在 Windows 上 `open(target, "a")` 会失败但 POSIX 同样成功——与 1 等价但更脏。排除。
3. **目录级测试文件**：只能测目录可写，测不到目标文件位。排除。

倾向：形态 1（存在即检查，不存在则照常创建）。

### 2.3 C：测试改造思路（不属于实施计划，仅为方案完整性）

- 锁定用例：改为「先经真实协议取得锁（等价于外部进程持有），再调 writer」——writer 必须失败且旧文件原样。不再用存活 r+ 句柄。
- 只读用例：保持 chmod 形态不变（POSIX 上由新增前置检查接住；Windows 上双保险：位检查或 replace 异常任一命中即 fail）。
- 两用例都必须在 Linux 与 Windows 上同语义通过；不得按平台分叉断言、不得 skip（任务红线）。

---

## 2.4 锁粒度与并发模型（方案可行性前提，实读 service.py:3234-3273）

- 调用点唯一：`session/service.py::refresh_progress_file`（L171 导入；L1141/L2492/L3322 触发：create_and_launch、task 五命令、run 终态），经 `anyio.to_thread.run_sync` 线程化执行（L3262-3264）。
- 跨进程：workspace 级已有 `InstanceLock`（instance_lock.py）单实例约束；本仓的进程模型下 progress writer 同一时刻单进程单线程触发。
- 因此 progress 侧锁是**防御外部/未来的并发形态 + 承载“外部锁定”可判定语义**，不是现有竞态修复——命名与注释必须如实（防过度设计），锁文件随目录生命周期管理。

---

## 3. 复用判定（口径 = Reuse Matrix §1，逐候选）

### 3.1 `filelock`（PyPI，tox-dev 维护）—— REUSE 与否

- **机制摘要**（既有知识，待核实）：对锁文件取 OS 排他锁（POSIX flock / Windows msvcrt），提供超时、计数、上下文管理器；锁文件即协议锚点。
- **契合点**：机制与仓内 `instance_lock.py` 同构；引入后 progress 写入器多一个运行时依赖，且 Vision §2.5 明令不为几行代码加新依赖（AGENTS.md §9.5 阶梯第 5 级）；零能力增益（仓内实现已覆盖同协议）。
- **判定**：**不复用（走仓内既有协议形态）**——不是 filelock 不合格，而是仓内已有同构且更轻的实现，加依赖不减少任何代码路径。

### 3.2 `portalocker` / `atomicwrites` / 强制锁 —— 同上分析

- `portalocker`：同 §3.1 判定（同为依赖，无增益）。
- `atomicwrites`（Python，已停更风险）：解决的是“原子替换”本身，本仓已用 `os.replace` 实现；不解决锁/只读位问题。排除。
- 强制锁 / 句柄探测：§2.1 已排除（不可移植 / 不可靠）。

### 3.3 仓内 `instance_lock.py` 锁协议 —— **PORT DESIGN（协议形态）+ 不直接 REUSE 类本身**

- **机制摘要**（instance_lock.py:101-124 实读）：双平台分支的 OS advisory 锁；非阻塞；OS 兜底释放。
- **为什么不直接 REUSE `InstanceLock` 类**：其语义绑定 workspace root 单实例（含逃生门、fence、租约、进程内注册表），与 progress 目录的“单文件写入权”语义不同；复用会引入与其逃生门/注册表机制的耦合，扩大架构面（违反 §8 Scope Lock）。**PORT DESIGN 的是协议形态**（flock/msvcrt 双平台分支 + 非阻塞 + OS 兜底释放 + advisory 边界声明），落点为 progress.py 内的小型私有 helper（或同目录新模块）。
- **License**：本仓自有代码，无外部复制。

### 3.4 只读前置检查（`os.access` / stat 写位）—— BUILD（标准库一行级）

- 标准库 `os.access`/`stat` 已覆盖（阶梯第 3 级）；无库可复用；属于写入器契约的扩展而非新能力。Windows 现有 `PermissionError` 路径保留为第二道防线（不删除既有失败语义——§8 红线：不为通过测试删除 Failure 语义）。

---

## 4. 方案依据块（协议 §1.3 五字段）

> 状态：**不完全版**——本会话宿主权限阻断了 WebFetch/WebSearch/gh 与浅克隆条件，仅完成来源识别与本地代码实读；标注 ⚠ 的行施工前必须补核实。

- **来源（≥2 独立）**：
  1. ⚠️（既有知识，置信度高，未实读）`filelock` 官方文档（py-filelock.readthedocs.io）——lockfile + 平台分支（flock/msvcrt）+ 超时/计数语义；
  2. ⚠️（既有知识，置信度高）POSIX `rename(2)` 与 `flock(2)` 语义（man7.org）：rename 只查目录权限；flock 为 advisory、进程退出自动释放；
  3. ✅（本地实读）本仓 `src/agent_harness/instance_lock.py` L101-124（flock/msvcrt 双平台分支）+ L9-16（协议边界声明），commit 06ca56b1（当前 HEAD，`git rev-parse` 实测）；
  4. ⚠️ GitHub 官方文档：ubuntu-latest 托管 runner 非 root 用户（CI 权限检查有效性前提，§1.4）。
- **机制摘要**：成熟方案（filelock/portalocker/仓内 instance_lock）一致选择「同目录锁文件 + OS 级 advisory 排他锁（flock/msvcrt）+ 非阻塞 + 进程退出自动释放」；只读目标在 POSIX 上无一由原子替换原语代查，均为调用方前置检查。
- **契合点**：advisory 边界与 AGENTS.md §7 不变量无冲突（progress writer 仍是唯一执行路径，不变量 7/8 不动；锁是 writer 内部细节，不新增第二执行路径）；advisory-only 意味着不遵守协议的外部程序不被约束——这与 W-05 票面「外部只读/锁定目录出现明确失败」的**可测语义**不矛盾：测试本身就是协议遵守方。新增锁文件名必须避开 `_TMP_PREFIX = "progress.md."` 的清理 glob（progress.py:572-579 会清 `progress.md.*`，锁文件命名需在契约上明确豁免或另名），否则下次写入会误删外部锁文件。
- **判定**：锁协议 = **PORT DESIGN**（Port 仓内 instance_lock.py 的 flock/msvcrt 协议形态到 progress 目录，不复用类本身、不引新依赖）；只读前置检查 = **BUILD**（标准库 `os.access`/`stat`，阶梯第 3 级）；`filelock`/`portalocker` = **DEFER**（零能力增益的新依赖）；测试模拟外部锁 = **ADAPT**（改走真实协议，非弱化）。
- **License**：零新依赖、零外部复制，无 License 义务。

---

## 4.1 推荐方案（一句话 + 三点形态）

**推荐：PORT DESIGN 仓内 instance_lock.py 的双平台 advisory 锁协议到 progress 目录（同目录锁文件 + flock/msvcrt 非阻塞排他锁，进程退出 OS 兜底），叠加写前 `os.access(target, W_OK)` 只读检查；测试的“外部锁定”改用同一锁协议模拟，不再用 Windows 句柄把戏。**

- 锁协议形态：progress 目录内独立锁文件（命名避开 `_TMP_PREFIX` glob），writer 写前取非阻塞排他锁，被占用 → `fail("locked", ...)`，拿不到不重试不等待（与现有 fail-fast 失败语义一致）；OS 兜底释放，无陈旧锁清理路径。
- 只读检查形态：目标存在时先查写权限，不可写 → 明确失败（`error_kind` 归入现有 `locked`/`env` 语义族，不新增枚举）；root 恒放行不误伤（§1.4）。
- 测试改造思路：锁定用例改为经真实协议持锁后调 writer；只读用例保持 chmod 形态；两用例同语义跨平台绿，不弱化断言。

## 4.2 最大不确定点（诚实列举）

1. **来源核实未完成**（最大项）：§4 的外部来源均为模型既有知识，未按 §6.1 用本地浅克隆 file:line+commit 或 WebFetch 实读核实；filelock/portalocker 的机制描述若与当前上游版本有漂移，判定不变（机制同构于仓内已验证实现），但施工票必须补核实记录。
2. **CI 用户身份**：非 root 未经实读确认（§1.4）；若 CI 实为 root，只读前置检查在 CI 恒放行，该用例需另找可移植的失败注入形态（此为方向 2 能否在 CI 变绿的前提，施工票第一优先核实）。
3. **`os.access` 在 Windows 的只读位语义**：既有知识认为 Windows 上 `os.access(path, W_OK)` 对只读位返回 False（能拦截），但 CPython 版本间有行为调整记录，施工票须以实测钉住，不能只凭文档。
4. **锁文件与 `_cleanup_stale_tmp` 的交互**：锁文件命名必须落在 `progress.md.*` 清理 glob 之外，否则下一次写入会误删外部锁文件、架空协议（§4 契合点已列，施工时须有测试钉住）。
5. **W-06/W-12 的触发点扩展**：refresh_progress_file 的 docstring（service.py:3246-3247）写明暂停前/压缩前触发点归 W-06/W-12——锁协议落地后这些未来调用点自动继承语义，无需改动，但施工票应验证这一假设而非顺手扩展。

---

## 5. 宿主权限阻塞记录（附录：尝试与结果）

| 尝试 | 结果 |
| --- | --- |
| `gh issue view 660` | 被宿主拒绝（审批未放行）——issue 原文未实读，用 Tracker/PHASE_STATUS 交叉印证 |
| `python3 -c`（os.replace 语义复现实验） | 被宿主拒绝——§1.2 的 POSIX 事实未在本机实测，为内核公开语义（置信度高） |
| `.venv/bin/pytest`（本机复跑两用例取红证） | 被宿主拒绝 |
| WebFetch / WebSearch（filelock、POSIX man、GitHub runner 文档） | 均被宿主拒绝——§6.1 来源核实未完成 |
| 仓库外 `ls`（本地浅克隆查找） | 被宿主限制（工作目录白名单外） |

结论：本地代码/文档面的证据链完整；外部来源核实与 CI 身份核实两个动作被宿主权限阻塞，已按 §9.1.1 停在报告阶段（未施工）。下游施工票开工前需：① 补 §6.1 来源核实；② 确认 CI runner 非 root。

---

## 6. §6.1 补核实（Phase 2 施工时补，2026-10-04）

Phase 1 报告文首声明的两项未完成核实，施工会话已补齐；置信度如实标注。

### 6.1 filelock 机制一句话核实 —— ✅ 实读完成（置信度：高，原文引用）

来源（`raw.githubusercontent.com/tox-dev/py-filelock` main 分支，2026-10-04 WebFetch 实读）：

1. `src/filelock/_unix.py`：POSIX 侧**确为**非阻塞排他 flock——原文引用：``fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)``（`_lock_fd_nonblocking` 内，注释明写 "One nonblocking exclusive flock attempt"）；
2. `src/filelock/_windows.py`：Windows 侧**更正 Phase 1 既有知识**——不是 `msvcrt.locking` 而是 Win32 `LockFileEx`（ctypes 直调 kernel32），原文引用：``_kernel32.LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, …)``，flags = `LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY`（非阻塞排他）；放弃 msvcrt 的上游自述理由："msvcrt.locking starts at the current position instead, so a metadata write between lock and unlock could shift the byte a later unlock targets"。

一句话机制：filelock = 对同目录锁文件取 OS 级**非阻塞排他锁**（POSIX `fcntl.flock` / Windows `LockFileEx`），锁文件即协议锚点，超时/重入是外围封装。**判定不变**：机制与本仓 `instance_lock.py` 同构（POSIX 分支逐字同 API），零能力增益 ⇒ `DEFER` 维持（§3.1）；Phase 1 §2.1 表中"同 flock/msvcrt 方案"的 Windows 侧描述按本次实读更正为"同 flock/LockFileEx 方案"。

### 6.2 CI 非 root 依据 —— ✅（我方已核实 + 官方文档间接佐证 + 本地机制探针实测）

- **gate1.yml 实读**（本仓 `.github/workflows/gate1.yml` L45/L82）：backend 与 frontend 车道均 `runs-on: ubuntu-latest`；
- **非 root 结论**（**我方已核实**，用户 Phase 2 任务提示词预先核实并要求注明）：ubuntu-latest 托管 runner 默认以非 root 用户 `runner` 执行 ⇒ `os.access(target, W_OK)` 前置检查在 CI 有效；
- **官方文档间接佐证**（2026-10-04 WebFetch 实读，docs.github.com hosted runners 页）：Linux/macOS VM "run using passwordless sudo"——root 无需 sudo，故该口径蕴含非 root；⚠ 诚实标注：`runner` 用户名本身未能从官方页面直接引出（readthedocs/runner-images 多次抓取超时或 404），该字段置信度仍为"高（我方核实 + 间接佐证）"而非"文档原文引用"；
- **本地机制探针实测**（硬证据，2026-10-04，本机 root + `su nobody` uid=65534）：对同一 0444 文件，uid=65534 ⇒ `os.access(W_OK) == False`（检查拦截 ⇒ CI 只读用例绿）；uid=0 ⇒ `True`（检查放行 ⇒ 本地 root 只读用例假红为**预期环境假象**，非实现缺陷）。

### 6.3 施工结果对账（Phase 2 落点摘要）

锁协议已按 §4.1 推荐方案落地（`progress.py` 私有 helper `_take_write_lock`/`_release_write_lock`/`_acquire_write_lock`，锁文件 `.progress.md.lock` 避开 `_cleanup_stale_tmp` 的 `progress.md.*` glob——glob 语义经 fnmatch 探针实测：反例名 `progress.md.lock` 会命中、实际名不命中）；只读前置检查 `os.access(W_OK)` 归入现有 `locked` 语义族（不新增枚举）；Windows `PermissionError → fail("locked")` 路径保留为第二道防线。锁定用例改真实协议持锁后绿；只读用例本地 root 假红（预期）、CI 非 root 绿。
