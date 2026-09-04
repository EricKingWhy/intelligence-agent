"""固定 cl100k_base 文本计数；其他模型的原生 token 数可能不同。"""

import tiktoken


def estimate_tokens(text: str) -> int:
    """估算文本 token 数（编码由 tiktoken 缓存）。"""
    return len(tiktoken.get_encoding("cl100k_base").encode_ordinary(text))
