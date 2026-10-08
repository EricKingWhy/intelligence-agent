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
import threading

import tiktoken
from langchain_core.messages import AnyMessage
from pydantic_core import PydanticSerializationError

logger = logging.getLogger("agent_harness.context.tokens")

#: 一张图片的近似 token 成本（#824 / MM-03，AC7 / PRD D3）。
#:
#: **口径（显式记录）**：投影层的**标准图片块**
#: （`{"type":"image","file_id":...}`，`attachments.projection.image_content_block`）
#: 只带内容寻址 id，不带像素/字节尺寸——本仓在估算处按**固定近似值**计入图片开销，
#: 而不是按 Provider 像素公式精算。依据（≥2 独立来源）：
#:
#: - Pi `packages/ai/src/utils/estimate.ts:16,34`（MIT，commit `1b347794`）：
#:   图片按 `ESTIMATED_IMAGE_CHARS = 4800` 字符计 → `4800 / 4 = 1200` token/图。
#: - Open WebUI 的图片预算下界参照 = 1000 token/图（`docs/research/
#:   2026-10-07-multimodal-image-input-research.md` §3.2 第 6 条）。
#: - DSH 有更精确的 DeepSeek 像素公式（`packages/llm/llm-deepseek/
#:   src/image-tokens.ts`，MIT，commit `5badb150`：14px patch、3:1 下采样、封顶
#:   1024 token），但需要图片像素尺寸，而标准块刻意不携带（加字段会破坏跨端投影
#:   契约，见 `attachments/projection.py` 的块形状）；故取"下界 + 常量"这一更稳的口径。
#:
#: **目的**：防止"图不计费导致 hard guard 失守"——多张大图会让估算显著抬高，
#: 不再让 `max_context_tokens * hard_guard_threshold` 对图片开销视而不见。真实
#: usage 仍以 Provider 回执为权威（`builder._usage_anchored_tokens` 的锚价只抬高）。
IMAGE_TOKENS_PER_IMAGE = 1200

#: 图片内容块的判别值：标准块（投影后 / 装配前）与 provider 块（装配后）。
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


def image_tokens_in_message(message: AnyMessage) -> int:
    """消息内容里的图片块数 × 单图近似成本（#824 / MM-03，AC7）。

    识别**标准块**（`{"type":"image",...}`，投影后、装配前）与 **provider 块**
    （`{"type":"image_url",...}`，装配后）两种形态；纯文本（`str` 内容）或只含
    文本块的列表一律 0——无附件的纯文本链路 token 口径逐字不变（AC9）。
    成本常量与口径见 `IMAGE_TOKENS_PER_IMAGE`。
    """
    content = message.content
    if not isinstance(content, list):
        return 0
    return sum(
        1
        for block in content
        if isinstance(block, dict) and block.get("type") in _IMAGE_BLOCK_TYPES
    ) * IMAGE_TOKENS_PER_IMAGE


def estimate_message_tokens(messages: list[AnyMessage]) -> int:
    """计入消息结构和 tool_calls；与文本估算使用同一个编码。

    图片口径（#824 / MM-03，AC7 / PRD D3）：带图消息里的标准图片块
    （`{"type":"image","file_id",...}`）在 `model_dump_json` 里只有几 token，真正
    的 base64 载荷在请求装配时才注入。为**不让图不计费击穿 hard guard**，本函数在
    结构 token 之外，按 `image_tokens_in_message` 追加每张图的近似成本（常量与来源
    见 `IMAGE_TOKENS_PER_IMAGE`）。纯文本消息该增量为 0，既有口径逐字不变（AC9）。

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
        total += estimate_tokens(payload) + image_tokens_in_message(message)
    return total
