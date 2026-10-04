# Runbook: tiktoken 离线缓存预置（#570）

## 背景

`agent_harness.context.tokens.estimate_tokens` 用 tiktoken 的 `cl100k_base`
做 token 估算（ADR-0007 子决策 1）。tiktoken 0.13 首次加载编码时从
`https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken`
下载（约 1.7MB）；air-gapped / 受限网络部署若未预置缓存，加载失败。
修复前该失败以未分类网络异常（ProxyError / SSLError）裸逃逸出
context 构建路径，杀死 run（报告 UI-02 / L-13，机制已在独立进程探针复现）。

## 行为契约（#570 修复后）

- **缓存命中**（含 `TIKTOKEN_CACHE_DIR` 预置）：精确计数，离线可用。
- **加载失败**（缺缓存需下载 / 缓存损坏重取失败 / 断网）：进程内锁定不可用，
  估算降级为 **UTF-8 字节数保守上界**——cl100k_base 是字节级 BPE（基词表 =
  256 个单字节 token），任何 token 至少占 1 字节 ⇒
  `tokens(text) <= len(text.encode("utf-8"))`，只可能多算、更早拦截预算 /
  压缩阈值，不会放行超窗口请求（spec 06 §8 hard guard 语义保持）；记一次
  WARNING，网络异常不再裸逃逸。重启进程（预置好缓存后）恢复精确计数。
- **绝不虚构低 token 数继续发送**：chars/4 类启发式对中文严重低估
  （实测语料「请帮我总结…待办事项」精确 23 tokens vs chars/4≈5），已按票面
  核查块纠偏删除该推定，未引入任何未证明保守边界的估算模式。

## 预置步骤（联网机预热 → 分发离线环境）

1. 联网机上用目标运行环境预热缓存目录：

   ```bash
   TIKTOKEN_CACHE_DIR=/srv/tiktoken-cache python -c \
     "import tiktoken; tiktoken.get_encoding('cl100k_base')"
   ```

2. 校验缓存文件存在（文件名 = blob URL 的 sha1）：

   ```bash
   python -c "import hashlib; print(hashlib.sha1(b'https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken').hexdigest())"
   ```

   tiktoken 读取缓存时按 registry 内嵌 sha256
   （`223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`）
   校验内容；坏文件自动删除并重取——真离线时重取失败即落入上界回退，
   不挂起、不崩溃。

3. 分发缓存目录到离线环境，运行时设置
   `TIKTOKEN_CACHE_DIR=/srv/tiktoken-cache`（tiktoken 原生读取该变量，
   本项目无需额外配置项；未设置时 tiktoken 回落 `DATA_GYM_CACHE_DIR` →
   系统临时目录 `data-gym-cache`）。
