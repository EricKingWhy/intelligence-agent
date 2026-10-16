# #1225：Windows 验证车道在云真机上的重跑读数（#1134 "将来重跑"兑现）

- 执行机：腾讯云 `Windows Server-jYPZ`（`lhins-ow7h2gkl`），Windows Server 2022 DataCenter 64bit CN，2 核 / 2GB
- 仓库：`origin/main` @ `41c9da6f`（2026-10-16；打包传输，非 git clone——GitHub 直连太慢）
- 工具链：Python 3.13.7（系统安装）/ uv 0.13.1（pip 安装）/ Git 2.51.0 / Node v22.23.3（zip 解压）
- 车道定义：`.github/workflows/mm08-d2d3-windows-verify.yml`（`workflow_dispatch`，本票为等价手工执行）
- 执行方式：SSH 密钥直连，命令与 workflow 一字不差（`uv run pytest … -q -p no:cacheprovider` / `npm ci` / `npm run check` / `npm test`）

## job `cli`：D2（Windows 字节发布不再是文本模式）

命令：`uv run pytest tests/test_cli_image.py tests/attachments/test_local_byte_store.py tests/test_instance_lock.py -q -p no:cacheprovider`

| 读数 | 判定 |
| --- | --- |
| `1 failed, 70 passed, 6 skipped in 99.60s` | 见下 |
| `tests/test_cli_image.py`：0 failed（修复前 2 failed） | ✅ D2 判据达成 |
| `tests/attachments/test_local_byte_store.py`：0 failed | ✅ |
| `tests/test_instance_lock.py`：1 failed（flake，见下）/ 其余 pass | ⚠️ |

### 唯一红：`test_lock_is_auto_released_when_holder_process_dies`（flake，非回归）

隔离证据链（全部在同一台真机、同一棵树 `41c9da6f` 上）：

1. 车道整跑：1 failed / 70 passed / 6 skipped（99.6s）
2. 该单测隔离跑：passed（0.37s）
3. `tests/test_instance_lock.py` 整文件重跑：24 passed（17.5s）
4. 该单测 30 次循环：**4/30 failed**（iter 12、21、23 及另一次）——复现率约 13%
5. 复刻脚本（spawn 持锁子进程 → kill → 立刻 acquire，5 轮）：5/5 立刻成功——竞态窗口小且罕见

根因：测试在 `proc.kill()` + `proc.wait()` 后**立刻**断言 `acquire()` 成功，不带任何重试。
Windows 上 `msvcrt.locking` 的字节区间锁在进程死后由 OS 释放，但释放可见性有微小延迟窗口；
约 13% 概率父进程的第一次 `LK_NBLCK` 撞上该窗口 → `OSError` → 误判为"锁未释放"。

定性：**测试的时序假设过强，不是产品回归**。产品的"进程死亡 OS 自动释放、不留残留锁"契约成立——
所有失败轮次在稍后重试都能拿到锁，从未出现永久性残留。修复方向是测试侧在 kill 后加短轮询，
已另开 issue 跟踪（本票按 #1225 约定只记录、不修）。

## job `tui`：D3（tui 全量在 Windows 上可移植）

| 步骤 | 读数 | 判定 |
| --- | --- | --- |
| `npm ci --no-audit --no-fund` | exit 0 | ✅ |
| `npm run check`（`tsc --noEmit`） | exit 0 | ✅ |
| `npm test` | `ℹ tests 248 / ℹ pass 246 / ℹ fail 0 / ℹ skipped 2`，exit 0 | ✅ D3 判据达成（`fail 0`） |

注：`tests` 计数 248（workflow 注释写 201 系旧数，主线已增长）；`skipped 2` 为 win32-skip
（与注释"另有 1 例 win32-skip"同类，`fail` 才是判据）。原始日志见同目录
`60-issue-1225-*.txt`（`cli-pytest.txt` / `tsc.txt` / `tui-full.txt`）。

## 对 #1134 的交代

#1134 把 `instance_lock.py` 的 Windows 车道表述改为"尚无 Windows 实测读数……若将来重跑再更新"。
本文件即"将来重跑"的读数：**D2、D3 判据在真机上双双达成**
（`test_cli_image.py` 0 failed；`npm test` fail 0）。
`instance_lock.py` 的相关注释/文档表述随本票更新为实测结论。
