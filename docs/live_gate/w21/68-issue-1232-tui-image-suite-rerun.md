# #1232 证据：TUI 图片族套件 Windows 重跑（#908 7 红已全绿）

## 背景

源自 #908（已关单）。#908 在 2026-10-09 的结论：TUI 图片族套件在 Windows 上 7 例红，全为测试自身的宿主平台耦合，不是产品 bug；且当时没有任何 CI workflow 提及 tui。

本票在新 Windows 真机（腾讯云 `Windows Server-jYPZ`，Windows Server 2022 DataCenter x64）上重跑确认。

## 关键发现：#908 的修复已全部落地

重跑前检查发现，当前 main（`10bc0ec3`）已包含 #908 建议的全部修复：

| #908 用例 | 位置 | 修复方式 | 当前代码 |
|---|---|---|---|
| 1-4（`.pathname` 前导斜杠） | `app-images.test.ts:28` | `fileURLToPath(new URL(...))` 取代 `.pathname` | ✅ 已修复 |
| 5（`fileURLToPath` 宿主绑定） | `image-paste.test.ts:62-69` | 按注入的 `platform` 构造期望值，不硬写字面 | ✅ 已修复 |
| 6（symlink EPERM） | `image-paste.test.ts:184-200` | `symlinkSkipReason()` 能力探测，失败则 skip 并打印原因 | ✅ 已修复 |
| 7（`pathToFileURL` 宿主绑定） | `image-view.test.ts:108-109` | 不写死 `file:///home/u/...`，按平台解析 | ✅ 已修复 |

## 重跑读数（2026-10-17，新 Windows 真机）

### 核心三文件（#908 的 7 例所在）

| 文件 | tests | pass | fail | skipped |
|---|---|---|---|---|
| `app-images.test.ts` | 30 | 30 | 0 | 0 |
| `image-paste.test.ts` | 24 | 22 | 0 | 2 |
| `image-view.test.ts` | 64 | 62 | 0 | 2 |

### 其余图片族文件

| 文件 | tests | pass | fail | skipped |
|---|---|---|---|---|
| `app-history-images.test.ts` | 4 | 4 | 0 | 0 |
| `app-image-limits.test.ts` | 7 | 7 | 0 | 0 |
| `clipboard-image.test.ts` | 13 | 13 | 0 | 0 |
| `image-media-types-snapshot.test.ts` | 4 | 4 | 0 | 0 |
| `mime-image-types.test.ts` | 2 | 2 | 0 | 0 |
| `pending-images.test.ts` | 10 | 10 | 0 | 0 |
| `clipboard-command.test.ts` | 6 | 6 | 0 | 0 |

### 全量 tui

```
ℹ tests 248
ℹ pass 246
ℹ fail 0
ℹ skipped 2
```

## 跳过项说明（均为预期）

1. `image-paste.test.ts` #16：POSIX FIFO（`mkfifo`）——Windows 无此能力，skip 并打印原因
2. `image-paste.test.ts` #20：字符设备（`/dev/zero`）——Windows 无此路径，skip
3. `image-view.test.ts` 的 2 个 skip：同类平台相关（详见测试输出）

**符号链接测试（原 #6）在本机真跑且通过**：本机以 Administrator 运行，`symlinkSync` 探针成功，测试未被跳过，直接 `ok`。

## 结论

1. **#908 的 7 例红在新真机上已全部转绿**，结论依然成立：原因为测试侧宿主耦合，现已修复
2. **无需再修测试**：#908 的三条建议（去平台耦合 / 能力探测跳过 / CI 车道）已全部实施
3. **未发现产品侧新问题**

## CI 车道建议

**已存在，无需新增**。`.github/workflows/gate-nightly.yml` 已有 `tui` job：

```yaml
tui:
  name: tui 单测（node:test, ${{ matrix.os }}）
  strategy:
    matrix:
      # #908 的 7 个红色用例是 Windows 宿主耦合，修好之后这条车道必须两个宿主
      # 都跑：只看 ubuntu 的话，"又写回 Windows-only 断言"在 CI 里永远是绿的。
      os: [ubuntu-latest, windows-latest]
```

该车道明确引用 #908，在 ubuntu 和 windows 双宿主上跑 `npm test`。建议：**保持现状**，无需新增 workflow。

## 环境

- 机器：腾讯云 `Windows Server-jYPZ`（`114.132.241.26`），Windows Server 2022 DataCenter x64
- Node：v22.23.3（`C:\node-v22.23.3-win-x64`）
- 源码：`C:\ia`，main `10bc0ec3`（经 #1230 刷新）
- 运行命令：`node --experimental-transform-types --test <files>` / `npm test`
