# #505 关单证据：冻结树全量 pytest 连续 3 轮（2026-10-11）

- **冻结树**：sha `f5e5bb5ee2b0c1ec21fea010b3865c299dd69f1c` / tree `11f2ff4e88eaebac18eaeebc58a0f40889929e4f`（= PR #962 合入后的 `main`；分支 `claude/505-stability-closure` 自该点起，三轮期间零提交）。
- **工作树**：每轮前后 `git status --short --untracked-files=all` 均为空（6/6 次）。
- **环境**：Linux 沙箱，Python 3.12.15，pytest 9.1.1；`.venv` 由 `uv sync --frozen --all-extras` 同步（`uv.lock` 未改）。收集 7439 例 / 51 deselected。
- **机读读数**：同目录 `505-stability-closure-f5e5bb5e.json`。原始日志与 junit 不入库（junit `.xml` 不在 `DOC_PATTERN`），sha256 前 16 位列于下表，仅作旁证。

## 命令（三轮逐字相同，串行，期间不跑其它重负载）

```bash
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
PYTHONUTF8=1 PYTEST_EXTRA_ARGS="-q --no-header -p no:cacheprovider -p no:randomly -rfE --junitxml=<per-round>.junit.xml" \
  timeout -s ABRT 2400 bash scripts/run_tests_clean.sh
```

`scripts/run_tests_clean.sh` = `PYTHONPATH= .venv/bin/python -m pytest tests/ $PYTEST_EXTRA_ARGS`（协议 §8.6 默认跑法）。`timeout -s ABRT 2400` 是每轮 40 分钟硬上限，三轮都没触发。

## 为什么要设 NO_PROXY（环境项，不是代码改动）

- **前一次尝试（同一冻结树，不设 NO_PROXY）**：第 1 轮打出 10 个 `F`，之后在 `tests/web/test_metrics.py::test_metrics_slow_resampling_does_not_block_event_loop` 卡住约 7 分钟（CPU 约 0），用 SIGABRT 终止，没有 junit。失败用例都会对真实 localhost HTTP 服务发请求（`test_context_usage`、`test_sse_disconnect`、MCP http transport、`test_approval_http_ack_kill`、`test_metrics`）。
- **机理**：沙箱设置了 `HTTP_PROXY` / `HTTPS_PROXY`，但没有回环地址豁免（`urllib.request.proxy_bypass('127.0.0.1') == False`）。httpx 客户端默认 `trust_env=True`，所以发往 `http://127.0.0.1` 的请求会走代理。
- **A/B 实证（主执行方，同一冻结树）**：7 个失败用例（`test_context_usage`、`test_sse_disconnect`、mcp http transport）不设 NO_PROXY 跑时 6 个失败；设了 `NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost` 后 **25 passed**。单独跑 `tests/web/test_metrics.py` 并设 NO_PROXY：**3 passed in 3.5s，不卡**。
- 结论：设 NO_PROXY 只是让发往回环地址的请求不走代理，相当于没有代理的网络条件；它不跳过任何用例，也不改任何断言。这次代理导致的红已记为**环境问题**，后续票由主执行方开。另有一个独立的测试设计问题，建议另票处理（是否开票由主执行方决定）：`test_metrics_slow_resampling` 的 `while not scan_started.is_set()` 等待循环没有超时，请求到不了服务端时会一直卡住，不会报失败。本次不修（Scope Lock）。

## 三轮读数

| 轮次 | 起止（CST） | rc | passed | failed | errors | skipped | pytest 墙钟 | 驱动墙钟 | junit sha256[:16] | log sha256[:16] |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 03:03:22–03:14:57 | 0 | 7399 | 0 | 0 | 40 | 655.73s | 695s | `8c534498d7b31436` | `3cea40d659d3dd3c` |
| 2 | 03:14:58–03:21:56 | 0 | 7399 | 0 | 0 | 40 | 413.65s | 418s | `ab57830053145adf` | `d1d521fd3d5779ab` |
| 3 | 03:21:57–03:30:51 | 0 | 7399 | 0 | 0 | 40 | 526.74s | 534s | `af20fe487e947f43` | `8bc88718d71f35d5` |

每轮 pytest 摘要行都是 `7399 passed, 40 skipped, 51 deselected`。junit 里 `tests=7439 failures=0 errors=0 skipped=40`。40 个 skip 三轮逐条相同（按 junit 的 node id + skip 原因比对）：Windows 专属 26、Docker SDK/daemon 不可用 12、本机无非回环 IPv4 1、langgraph 已安装（非最小 Core 口径）1。

之前因代理失败的那几组，在第 1 轮的 junit 里全部 passed：`test_client_lifecycle` 19、`test_approval_http_ack_kill` 1、`test_sse_disconnect` 2、`test_context_usage` 22、`test_metrics` 3。

## 两条目标用例

| 票 | node id | 第 1 轮 | 第 2 轮 | 第 3 轮 |
| --- | --- | --- | --- | --- |
| #376 | `tests/web/test_memory_api.py::test_v2_bulk_confirmation_settings_and_session_recall_redaction` | passed (0.748s) | passed (0.100s) | passed (0.107s) |
| #338 | `tests/web/test_memory_api.py::test_v2_list_filters_detail_versions_edit_stale_version_and_identity` | passed (0.098s) | passed (0.112s) | passed (0.118s) |

## 执行插曲（如实登记）

- 本组之前还有一次尝试，被作废：第 1 轮跑到约 81% 时沙箱虚拟机重启（`uptime` 归零），pytest 和驱动进程都没了，没有 junit 和 rc。已打印的结果里 0 F / 0 E。这不是测试失败或超时，所以从第 1 轮整组重新开始，上表就是重跑后连续完成的 3 轮。
