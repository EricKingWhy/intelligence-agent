"""固定 cl100k_base 文本计数；其他模型的原生 token 数可能不同。

#570：编码**加载**失败不再让网络异常裸逃逸杀死 run（UI-02：离线首次估算
抛 ProxyError/SSLError → run 在第一次模型调用前 run/failed）。加载失败
（缺缓存需下载 / 缓存损坏重取失败 / 断网）→ 记一次 WARNING（类型 + 原因 +
处置），进程内锁定不可用，估算回退到**已证明的保守上界**：UTF-8 字节数。

上界证明：cl100k_base 是字节级 BPE，基词表 = 256 个单字节 token，任何
token 至少占 1 字节 ⇒ ``tokens(text) <= len(text.encode("utf-8"))``。对
protected-facts 预算 / 压缩 / 外置阈值这是**保守方向**（只可能多算、更早
拦截，不会放行超窗口请求——spec 06 §8 hard guard 语义保持）；也绝不虚构
更低的"精确值"继续发送（票面核查块纠偏：chars/4 对中文严重低估，不是
硬护栏安全兜底，已删除该推定）。

离线部署的精确计数路径：预置 ``TIKTOKEN_CACHE_DIR`` 指向预热的编码缓存
（tiktoken 0.13 ``load.py``：缓存目录优先级 TIKTOKEN_CACHE_DIR →
DATA_GYM_CACHE_DIR → 临时目录；缓存键 = blob URL 的 sha1；读取时按
registry 内嵌 sha256 校验，坏缓存自动删除并重取——真离线时重取失败即
落入本文件的上界回退，不挂起、不崩溃）。
"""

from __future__ import annotations

import logging
import math
import threading

import tiktoken
from langchain_core.messages import AnyMessage
from pydantic_core import PydanticSerializationError

logger = logging.getLogger("agent_harness.context.tokens")

#: 图片 token 的**尺寸相关近似公式**（#935 / M-03，替代 #824 的固定 1200 常量）。
#:
#: **口径（显式记录）**：一张图的近似成本 = `IMAGE_TOKENS_BASE +
#: IMAGE_TOKENS_PER_TILE × (tiles)`，其中
#: `tiles = ceil(w / IMAGE_TILE_PX) × ceil(h / IMAGE_TILE_PX)`（长边先按
#: `IMAGE_MAX_DIMENSION` **等比**缩小，与发送前归一化同语义）。三家主流 Provider 全部用
#: 尺寸相关公式、无一家用固定常量，本仓取**最简的 tile 制**作统一近似（不逐 Provider
#: 精算），来源（≥2 独立来源，官方原文核对见 tracked 文档
#: `docs/tickets/multimodal-2026-10-07/M-03-image-token-sizing.md`）：
#:
#: - **OpenAI** Images 指南（`developers.openai.com/api/docs/guides/images`，章节
#:   "Calculating costs" → "Tile-based image tokenization"）：`gpt-4o`/`gpt-4.1`
#:   **base 85 + tile 170**，按 **512px** 方块计数；低细节固定 = base。
#: - **Gemini**（Firebase AI Logic《Count tokens for Gemini models》，"Image input
#:   files"）：小图（两维 ≤384px）= 258；大图按 **768×768** tile、每 tile 258。
#: - **Anthropic**（`platform.claude.com/.../vision`，"Image limits and costs"）：
#:   `⌈w/28⌉ × ⌈h/28⌉`（28×28 patch），上限 1568/4784——同为尺寸相关（旧版为
#:   `(w×h)/750`、上限 1600，现已被 patch 制取代）。
#:
#: **为什么这些常数**：取 OpenAI 的 512px tile + 85/170 是因为本仓请求装配发的正是
#: OpenAI 风格 `image_url` 块（`model/multimodal.py`），其成本模型最贴合本仓实际
#: Provider 面。**简化之处**：不复制 OpenAI 的"先缩进 2048²、再把短边压到 768"两段
#: 预处理（那是逐 Provider 精算）；只用一步**长边等比缩放**到 `IMAGE_MAX_DIMENSION`
#: （与发送前归一化 `attachments.normalize` 的 `frame.thumbnail((N,N))` **近似同语义**：
#: 同为长边等比、保持长宽比，但**取整判据不同**——PIL thumbnail 按长宽比误差最小挑
#: 整数并保下界 1，估算侧用 `math.ceil` 取保守上界（`max(1, ...)`），不声称逐像素等价）。
#: 取整分叉真正出现在**长短边比接近 4:1/2:1 且缩放商落在整数边界下侧的常规尺寸**（如
#: 4096×1025、3073×769，Round 1 已实测两侧 tile 数不同）；2048×100 实测估算与 PIL 一致
#: （765=765）——这是已披露的简化代价，不追求逐 Provider 精确。
#:
#: **尺寸来源**：标准图片块（`attachments.projection.image_content_block`）**携带
#: `width`/`height`**（该数据在投影处即 ImageRef 的字段，随块一起带给估算层），故本
#: 估算保持**消息的纯函数**——全部估算调用点（builder / compactor / service / 各
#: Provider）自动同口径，无需任何 session 反查管道。（设计取舍见 tracked 文档
#: `docs/tickets/multimodal-2026-10-07/M-03-image-token-sizing.md`。）
#:
#: **目的**：防止"图不计费导致 hard guard 失守"——多张大图会让估算随尺寸单调抬高；
#: 同时不再对大图系统性低估（旧 1200 常量在 2048² 归一化上限下，相对 Anthropic patch
#: 口径 `⌈2048/28⌉²=5476`——封顶后 4784——低估约 4.0–4.6 倍，两种基准结论一致）。真实
#: usage 仍以 Provider 回执为权威（`builder._usage_anchored_tokens` 的锚价只抬高）。
IMAGE_TILE_PX = 512

#: tile 制的 base / per-tile（OpenAI `gpt-4o`/`gpt-4.1` 行）。
IMAGE_TOKENS_BASE = 85
IMAGE_TOKENS_PER_TILE = 170

#: 长边归一化上限（与 `attachments.normalize.TARGET_MAX_DIMENSION` **同值**：发送前
#: 长边 ≤ 2048px）。估算按此做**等比**缩放（与 `frame.thumbnail((N,N))` 近似同语义、
#: 取整判据不同），不逐
#: Provider 精算其缩放阶梯。
#:
#: **同步义务（保守性前提）**：运行期实际上限由可配置的
#: `Settings.image_normalize_max_dimension`（`agent_harness/config.py`，默认 2048）决定；
#: 本常量必须 **≥** 该设置值，否则"尺寸未知回退 = 上限²"的保守契约会在设置被调大时
#: 静默击穿。改动其中之一，务必同步核对二者。
IMAGE_MAX_DIMENSION = 2048


def image_tokens_for_size(width: int, height: int) -> int:
    """单张 `width×height` 图的近似 token 成本（tile 制，见模块常量注释）。

    长边先**等比**缩放到 `IMAGE_MAX_DIMENSION`（与发送前归一化 `frame.thumbnail` 近似
    同语义：同为长边等比、保持长宽比，取整判据不同），再按 `IMAGE_TILE_PX` 方块向上取整计数；退化尺寸（0）只计
    base。纯函数、确定性。
    """
    w = max(0, int(width))
    h = max(0, int(height))
    longest = max(w, h)
    if longest > IMAGE_MAX_DIMENSION:
        scale = IMAGE_MAX_DIMENSION / longest
        w = max(1, math.ceil(w * scale))
        h = max(1, math.ceil(h * scale))
    tiles = math.ceil(w / IMAGE_TILE_PX) * math.ceil(h / IMAGE_TILE_PX)
    return IMAGE_TOKENS_BASE + IMAGE_TOKENS_PER_TILE * tiles


#: 尺寸**未知**时的回退值（provider 块 `image_url` 无尺寸字段、或块形状异常）。
#: 第一性原理：管线单图可发送的最大尺寸即 `IMAGE_MAX_DIMENSION²`（前提见 `IMAGE_MAX_DIMENSION`
#: 的同步义务：运行期 `image_normalize_max_dimension ≤ IMAGE_MAX_DIMENSION`）；未知尺寸时按
#: 该上限估是**保守方向**（只可能多算、更早拦截），与 hard guard 的 fail-closed 语义
#: 一致——绝不因"不知道多大"而少算图片开销。
IMAGE_TOKENS_UNKNOWN_SIZE = image_tokens_for_size(IMAGE_MAX_DIMENSION, IMAGE_MAX_DIMENSION)

#: 图片内容块的判别值：标准块（投影后 / 装配前）与 provider 块（装配后）。
#: 取值与 `attachments.projection.image_content_block`（产出 `"image"`）和
#: `model.multimodal._STANDARD_IMAGE_TYPE` / `_PROVIDER_IMAGE_TYPE`（`"image"` /
#: `"image_url"`）**同源**——这里是 context 层、不反向依赖 model 适配层，故以常量
#: 复述协议级 block type。两处若改其一，务必同步（本注解即同步义务登记）。
_IMAGE_BLOCK_TYPES = frozenset({"image", "image_url"})

#: 进程内「精确编码不可用」锁（#570）：tiktoken 只记忆**成功**的编码实例
#: （``registry.ENCODINGS``），失败后每次 ``get_encoding`` 都会重新走下载
#: 路径——run 内多次估算会把一次断网放大成 N 次慢网络重试（每次还各抛
#: 未分类异常）。首次失败即锁定，后续调用直接走上界估算；预置好缓存后
#: 重启进程即恢复精确计数。
_ENCODING_UNAVAILABLE: Exception | None = None

#: latch 的 check-set-warn 互斥（P3 残余修复）：并发首用时 N 个线程都先看到
#: ``_ENCODING_UNAVAILABLE is None`` 再各自失败——无锁时同一次断网被报 N 条
#: WARNING。锁只包失败路径的判定与置位；成功路径（含下载耗时）不持锁。
_LATCH_LOCK = threading.Lock()


def _load_encoding():
    """返回 cl100k_base 编码；加载失败锁定并抛原始异常（estimate_tokens 捕获回退）。"""
    global _ENCODING_UNAVAILABLE
    if _ENCODING_UNAVAILABLE is not None:
        raise _ENCODING_UNAVAILABLE
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception as error:  # 缺缓存/损坏/断网统一按「加载不可用」处置（再抛由调用方回退）
        with _LATCH_LOCK:
            first_failure = _ENCODING_UNAVAILABLE is None
            if first_failure:
                _ENCODING_UNAVAILABLE = error
        if first_failure:
            logger.warning(
                "cl100k_base 编码加载失败（%s: %s），token 估算降级为 UTF-8 字节数"
                "上界（仅保守方向偏离）；离线部署请预置 TIKTOKEN_CACHE_DIR 指向预热"
                "的编码缓存",
                type(error).__name__,
                error,
            )
        raise


def _utf8_byte_upper_bound(text: str) -> int:
    """cl100k_base 的**已证明上界**：UTF-8 字节数（字节级 BPE ⇒ tokens ≤ bytes）。"""
    return len(text.encode("utf-8"))


def estimate_tokens(text: str) -> int:
    """估算文本 token 数（编码由 tiktoken 缓存）。

    精确计数由 tiktoken cl100k_base 提供（缓存命中即离线可用）；编码加载
    不可用时返回已证明的保守上界（UTF-8 字节数）并记一次 WARNING——估算
    永不抛致命异常，也绝不虚构低 token 数继续发送。
    """
    try:
        encoding = _load_encoding()
    except Exception:  # noqa: BLE001 — 加载不可用是预期分支，回退已证明上界
        return _utf8_byte_upper_bound(text)
    return len(encoding.encode_ordinary(text))


def _contains_lone_surrogate(value: object) -> bool:
    """递归判定 JSON 兼容结构里是否含孤立代理项码点（U+D800–U+DFFF）。

    Python ``str`` 把 astral 字符存为**单个码点**、不存成对代理项，故任何
    落在代理区间的码点都是孤立的（AC3 正控：两段 surrogate 拼在一个 str
    里仍判非法）。dict / list / tuple 之外的分支不含字符，直接 False。
    """
    if isinstance(value, str):
        return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    if isinstance(value, dict):
        return any(
            _contains_lone_surrogate(key) or _contains_lone_surrogate(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_lone_surrogate(item) for item in value)
    return False


def _as_dimension(value: object) -> int | None:
    """`width`/`height` 取值：非负 int（`bool` 除外）才收，否则 `None`（走尺寸未知回退）。"""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def image_tokens_in_message(message: AnyMessage) -> int:
    """消息内容里的图片块按**尺寸相关近似**计费（#935 / M-03；替代 #824 的固定常量）。

    识别**标准块**（`{"type":"image",...,"width","height"}`，投影后、装配前）与
    **provider 块**（`{"type":"image_url",...}`，装配后）两种形态。标准块在投影处已带
    `width`/`height`（`attachments.projection.image_content_block`），按
    `image_tokens_for_size` 计；provider 块无尺寸字段（其形状由 provider 协议决定，
    不可加字段）⇒ 按 `IMAGE_TOKENS_UNKNOWN_SIZE` 保守回退。纯文本（`str` 内容）或只含
    文本块的列表一律 0——无附件的纯文本链路 token 口径逐字不变（AC9）。成本与口径见
    `IMAGE_TOKENS_BASE` / `IMAGE_TOKENS_PER_TILE` / `IMAGE_TILE_PX`。
    """
    content = message.content
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if not (isinstance(block, dict) and block.get("type") in _IMAGE_BLOCK_TYPES):
            continue
        width = _as_dimension(block.get("width"))
        height = _as_dimension(block.get("height"))
        if width is None or height is None:
            total += IMAGE_TOKENS_UNKNOWN_SIZE
        else:
            total += image_tokens_for_size(width, height)
    return total


def message_cost(message: AnyMessage, *, payload: str | None = None) -> int:
    """单条消息的 token 成本 = 结构 token + 图片近似成本（#935 / M-03）。

    结构 token 走 `estimate_tokens(model_dump_json)`（与全仓同一编码）；图片增量见
    `image_tokens_in_message` / `image_tokens_for_size`（尺寸相关近似）。预算/增量/锚三条
    估算路径共用本函数，避免"结构 + 图片"这一惯用式在四处各写一遍而漂移。`payload` 已由
    调用方算好时直接传入（`estimate_message_tokens` 要先拿它做 surrogate 校验，
    否则会重复序列化）。
    """
    if payload is None:
        payload = message.model_dump_json()
    return estimate_tokens(payload) + image_tokens_in_message(message)


def estimate_message_tokens(messages: list[AnyMessage]) -> int:
    """计入消息结构和 tool_calls；与文本估算使用同一个编码。

    图片口径（#935 / M-03，替代 #824 / PRD D3 的固定常量）：带图消息里的标准图片块
    （`{"type":"image","file_id",...,"width","height"}`）在 `model_dump_json` 里只有几 token，
    真正的 base64 载荷在请求装配时才注入。为**不让图不计费击穿 hard guard**，本函数在
    结构 token 之外，按 `image_tokens_in_message` 追加每张图的**尺寸相关近似**成本
    （公式与来源见 `IMAGE_TOKENS_BASE` 等常量）。纯文本消息该增量为 0，既有口径逐字不变（AC9）。

    #650：孤立 Unicode surrogate 能通过 Python 层校验、却无法编码进
    JSON——``model_dump_json`` 裸抛 ``PydanticSerializationError``（未收敛）。
    Context 预算边界把它映射为 ``ContextWindowExceededError``（spec 06 §8
    hard guard：停止或交用户处理，不放行、不静默降级）。映射前先用
    ``_contains_lone_surrogate`` 验证成因：非 surrogate 原因的序列化失败
    原样上抛，不与非法 Unicode 混类。错误消息只装定位与码点区间，
    不回显用户内容。
    """
    total = 0
    for index, message in enumerate(messages):
        try:
            payload = message.model_dump_json()
        except PydanticSerializationError as error:
            if not _contains_lone_surrogate(message.model_dump()):
                raise
            # 惰性导入：compactor 模块级导入本模块，模块级反向导入成环；
            # 调用时本模块已初始化完毕，取到与 builder/runtime 同一个类对象。
            from agent_harness.context.compactor import ContextWindowExceededError

            raise ContextWindowExceededError(
                f"messages[{index}] ({type(message).__name__}) 含孤立 Unicode "
                "代理项（U+D800–U+DFFF），无法序列化进 Context 预算；"
                "按 hard guard 语义拒绝本轮估算"
            ) from error
        total += message_cost(message, payload=payload)
    return total
