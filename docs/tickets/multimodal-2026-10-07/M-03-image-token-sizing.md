# M-03 图片 token 尺寸公式：来源核对与口径（#935）

> 本文是 #935 / M-03 的**方案依据**（tracked 载体）。代码注释
> `src/agent_harness/context/tokens.py` 的常量块引用本文。核对日期 2026-10-10，
> 核对人 CodeBuddy（执行器，非自审）。

## 裁决

用户 2026-10-10 裁决：图片 token 计量**采用尺寸相关的近似公式，废弃 #824 的固定
常量 `IMAGE_TOKENS_PER_IMAGE = 1200`**。为简化近似（不逐 Provider 精算），统一取
**OpenAI `gpt-4o` tile 制**作为本仓口径。

## 本仓口径（单一事实源 `context/tokens.py`）

```
tokens(width, height) = IMAGE_TOKENS_BASE(=85)
                      + IMAGE_TOKENS_PER_TILE(=170) × tiles
tiles = ceil(w' / 512) × ceil(h' / 512)
```

其中 `w', h'` 是长边先**等比**缩放到 `IMAGE_MAX_DIMENSION(=2048)` 后的尺寸——与发送
前归一化 `attachments.normalize`（`frame.thumbnail((N,N))`，保持长宽比）**同语义**。
尺寸未知（provider 块 `image_url` 无尺寸字段、或块形状异常）时回退
`IMAGE_TOKENS_UNKNOWN_SIZE = tokens(2048, 2048) = 2805`（保守方向，fail-closed）。

**同步义务**：`IMAGE_MAX_DIMENSION` 必须 ≥ 运行期 `Settings.image_normalize_max_dimension`
（默认 2048），否则"未知尺寸回退 = 上限²"的保守契约会被静默击穿。

## 三家主流产品公式一手来源核对（不抄票面二手数字）

票面（#935）给的二手数字：OpenAI `85 + 170×tiles`；Anthropic `(w×h)/750` 上限 1600；
Gemini `258 基础 + tile 制`。**逐条去官方原文核对**如下——两家与票面有出入，已按原文更正：

### OpenAI —— 与票面一致
- 来源：`https://developers.openai.com/api/docs/guides/images`（原
  `platform.openai.com/docs/guides/vision` 重定向至此），页标题 "Images"，章节
  "Calculating costs" → "Tile-based image tokenization"。
- 核对原文（verbatim）：
  > "With `detail: high` or `detail: auto` … Count the 512px squares needed to cover the
  > image. Each square uses the model's tile tokens. Add the model's base tokens to the
  > tile tokens."
- 表格：`gpt-4o` / `gpt-4.1` → **Base tokens 85 / Tile tokens 170**；低细节 →
  "an image costs only the model's base tokens, regardless of dimensions"（= 85）。
- **结论：票面 `85 + 170×tiles` 与官方一致（针对 gpt-4o/gpt-4.1）。**

### Anthropic —— 票面为旧版/近似，原文已改为 patch 制
- 来源：`https://platform.claude.com/docs/en/docs/build-with-claude/vision`（原
  `docs.anthropic.com` 重定向至此），章节 "Image limits and costs" →
  "Resolution and token cost"。
- 核对原文（verbatim）：
  > "Claude views images in patches instead of pixels. Each patch is a 28×28-pixel block …
  > An image, therefore, costs `⌈width / 28⌉ × ⌈height / 28⌉` visual tokens."
- 上限表：Standard 1568 / High-resolution 4784。
- **结论：票面「(w×h)/750 上限 1600」在当前官方文档中未出现**——现行为 28×28 patch 制；
  `w*h/784 ≈ w*h/750` 与其近似等价，旧 1600 cap 近似现 1568。票面数字是旧版/近似口径，
  **本质仍是尺寸相关（裁决方向不变），但原文核对以现行 patch 公式为准。**

### Gemini (Firebase AI Logic) —— 票面口径不精确
- 来源：Firebase AI Logic《Count tokens for Gemini models》
  `https://firebase.google.cn/docs/ai-logic/count-tokens`（Google 官方），章节
  "Count multimodal input tokens" → "Image input files"。
- 核对原文（verbatim）：
  > "Image inputs with both dimensions less than or equal to 384 pixels: each image is
  > counted as 258 tokens."
  > "Image inputs that are larger in one or both dimensions: each image is cropped and
  > scaled as needed into tiles of 768x768 pixels, and then each tile is counted as 258
  > tokens."
- **结论：票面「258 基础 + tile 制」不精确**：官方是「小图固定 258 / 大图按 768² tile
  × 258（无额外加法基础）」。

### 三源结论
三家**均为尺寸相关**公式，无一家用固定常量——与用户裁决方向一致。故本仓取最简的
OpenAI tile 制作统一近似；Anthropic / Gemini 的票面数字是二手/旧版，本票已按官方原文
改记（上文）。

## 与旧固定常量（1200）的消融对照

同组图（1 张小图 8×6 + 1 张 2048² 大图），旧口径（固定 1200）vs 新口径：

| 样本 | 旧（1200） | 新公式 | 偏差（相对本仓口径） |
| --- | --- | --- | --- |
| 小图 8×6 | 1200 | 255 | 旧高估 4.71× |
| 大图 2048² | 1200 | 2805 | 旧低估 2.34× |

证据脚本与输出：`~/workspace/system/dispatch/935-ablation/ablate_image_tokens.py` /
`ablation-output.txt`（留盘）。结论：旧固定常量在两端都错，新公式随尺寸单调、两端都
相对**本仓口径**修正（不声称"更接近 Provider 真值"——对极端尺寸本仓仍是简化近似）。
