# 下一会话必读（2026-09-16 规则改造 + 集成批次之后）

> 本文件是一次性交接说明，不是长期规则。长期规则已写进 `AGENTS.md` / `CLAUDE.md` /
> `docs/SDD_WORKFLOW_PROTOCOL.md`。读完本节后可以只看那三份。

## 0. 一句话现状

三个仓库**已经全部停在 `main` 的同一个 commit**（`3732224`），工作树干净，`origin/main` 已与之同步。
规则文件、SDD 协议、CLAUDE.md 都已按新模型对齐。

```bash
for d in /d/intelligence-agent /d/intelligence-agent-backend /d/intelligence-agent-frontend; do
  echo "$d $(git -C $d rev-parse --abbrev-ref HEAD) $(git -C $d rev-parse --short HEAD)"
done
# 三个都应是 main 3732224
```

## 1. ⚠️ 一个还活着的进程会污染门禁（先处理它）

**端口 5173 上有一个遗留的 vite dev server（PID 11460），来自更早的某次 playwright 运行，
不是本轮起的，本轮也没有终止它。**

后果：`web/playwright.config.ts` 的 `reuseExistingServer: !process.env.CI` 会**静默复用**
这个不是本次运行起的 server。跨 clone 复用会让 e2e 在"看起来跑了"的情况下考错对象——
**已经造成过一次 18 条假红**（详见 issue #209）。

跑 e2e 之前，二选一：

```bash
# 路线 A：确认没有别的 server 占着 5173（最省事）
netstat -ano | grep ':5173'          # 有 LISTENING 就先确认那是谁

# 路线 B：用独立端口跑，绕开复用（本轮用的办法）
#   临时 config 把 baseURL/webServer 换成 5273 + reuseExistingServer:false，跑完即删
```

判据：**你跑的那一次，webServer 必须是 playwright 自己起的。**

## 2. 仓库模型变了：别再往 feature 分支上长期挂

- 以前：backend clone 常驻 `feat/backend`、frontend clone 常驻 `integrate/ws-stream`。
- 现在：**三个 clone 平时都在 `main`**；干活时开短分支，合回 main 即结束。

旧分支还在（`feat/backend`、`integrate/ws-stream`、`feat/frontend` 等），
**都已无独有 commit**（工作全部进入 main），属于休眠遗留。删除需用户批准，本轮未删。

集成后有一条新纪律（`AGENTS.md` §14.9）：**开工前先自检**

```bash
git merge-base --is-ancestor main HEAD && echo "不落后" || echo "落后，先把 main 合回来"
```

## 3. 本轮改了什么（细节见 `docs/PHASE_STATUS.md` 的 2026-09-16 条目）

| 类 | 提交 |
| --- | --- |
| 角色模型去工具名绑定（Primary = 谁当前在干活） | `2c172ca` |
| 规格路径 `SPEC_ROOT` 化 + `docs/spec/README.md` | `6644a32` |
| §16 降级为入口；协议文件补上它从未写过的显式取代声明 | `db545b6` |
| 仓库模型改「三个独立 clone + 平时都在 main」+ 集成后回补 + §14.13 跨仓库两硬规则 + §14.4 授权分级 | `6a07979` |
| §10 静态 skill 清单改动态发现 | `a7472cd` |
| `CLAUDE.md` 薄化 637 → 169 行（红线 + 指针） | `ed364c2` |
| 审计手册勘误段（两处结论被证伪） | `ef20e91` |
| 在途分支集成（backend 3 commit / frontend 7 commit = #207） | `44c6326` / `074a2bb` / `34c2251` |

**最重要的一条事实更正**：三个仓库的 `AGENTS.md` / `CLAUDE.md` **从来没有漂移过**——
git blob 三处完全相同。此前判定的"漂移"是 `core.autocrlf` 差异（main/backend 检出 CRLF、
frontend 检出 LF）造成的**工作树字节差异**。`web/` 同理不是两份拷贝，是同一个 tracked 目录的
三份 checkout，且 `feat/backend` 从未提交过任何 `web/` 改动。

**推论**：跨 clone 比较一律比 git 对象，不要比工作树字节（`AGENTS.md` §13.1 / §14.13(b)）。

## 4. 等用户拍板的事（不要自己决定）

| # | 事项 | 需要什么 |
| --- | --- | --- |
| #210 | 行尾策略：是否统一 `eol=lf`，或只保留文档警告 | 跨三仓库策略改动，影响面大 |
| #211 | `docs/spec/` 是否改名（实测 38 文件 107 处引用） | 改名要动 38 个文件且跨仓库 |
| #208 | WS 快照 backlog 上限 / `stream/truncated` 在 WS 主通道上不可达 | 需要 A（复用 SSE 阈值）或 B（快照分帧）二选一 |
| #199 / #201 | 故意保持 OPEN：票面各有**冻结 AC 缺数据源**未落地 | 需要后端补数据源，不是前端能补的 |

## 5. 本轮如实划界的未做项

`docs/INTEGRATION_PROMPT_WS_COMPLETE.md` §4 列的**真机验证点**（交付层不攒 WS 帧、队列立即出字、
断线重连横幅、代理拒 WS 时降级）**本轮没做**：它们依赖真实部署环境（腾讯 EdgeOne 交付层），
本地造不出来。自动化门禁全绿，但这四项建议在能起完整环境时补扫。
