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

import tiktoken
from langchain_core.messages import AnyMessage

logger = logging.getLogger("agent_harness.context.tokens")

#: 进程内「精确编码不可用」锁（#570）：tiktoken 只记忆**成功**的编码实例
#: （``registry.ENCODINGS``），失败后每次 ``get_encoding`` 都会重新走下载
#: 路径——run 内多次估算会把一次断网放大成 N 次慢网络重试（每次还各抛
#: 未分类异常）。首次失败即锁定，后续调用直接走上界估算；预置好缓存后
#: 重启进程即恢复精确计数。
_ENCODING_UNAVAILABLE: Exception | None = None


def _load_encoding():
    """返回 cl100k_base 编码；加载失败锁定并抛原始异常（estimate_tokens 捕获回退）。"""
    global _ENCODING_UNAVAILABLE
    if _ENCODING_UNAVAILABLE is not None:
        raise _ENCODING_UNAVAILABLE
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception as error:  # 缺缓存/损坏/断网统一按「加载不可用」处置（再抛由调用方回退）
        _ENCODING_UNAVAILABLE = error
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


def estimate_message_tokens(messages: list[AnyMessage]) -> int:
    """计入消息结构和 tool_calls；与文本估算使用同一个编码。"""
    return sum(estimate_tokens(message.model_dump_json()) for message in messages)
