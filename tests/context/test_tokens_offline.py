"""#570：tiktoken 编码加载不可用时，估算不得裸逃逸、不得虚构低值。

四条验收线（票面核查块）：

1. 加载失败（缺缓存需下载 / 损坏重取失败 / 断网）→ 估算回退**已证明的保守
   上界**（UTF-8 字节数），网络异常不再以未分类形态裸逃逸（UI-02：原样
   ProxyError 杀 run）；
2. 上界证明：cl100k_base 字节级 BPE ⇒ tokens ≤ UTF-8 字节数——语料实测
   精确计数逐条落在界内，且不被 chars/4 类低值顶替（纠偏：chars/4 对中文
   严重低估，不是硬护栏安全兜底）；
3. 首启断网：空缓存目录 + 断网 → 上界回退 + WARNING，进程内锁定不再重复
   下载重试（tiktoken 只记忆成功编码，不锁会把一次断网放大成 N 次慢重试）；
4. 预置校验过的离线缓存（TIKTOKEN_CACHE_DIR）→ 精确计数，零网络访问。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest
import tiktoken
import tiktoken.load
from langchain_core.messages import HumanMessage

from agent_harness.context import tokens as tokens_module
from agent_harness.context.tokens import estimate_message_tokens, estimate_tokens

_STORE_LOGGER = "agent_harness.context.tokens"

#: AC 语料（UI-02）：中文 / emoji / 代码 / 工具 schema。cl100k_base 冻结，
#: 精确计数稳定（tiktoken 0.13.0 实测锚点）。
CORPUS = {
    "chinese": ("请帮我总结今天的工作内容并列出明天的待办事项", 23),
    "emoji": ("🎉🎊🎈✨🥳", 14),
    "code": ("def f(x):\n    return x * 2", 10),
    "tool-schema": (
        (
            '{"name":"get_weather","parameters":{"type":"object",'
            '"properties":{"city":{"type":"string"}}}}'
        ),
        21,
    ),
}

BLOB_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
#: registry 内嵌的 blob sha256（openai_public.cl100k_base）。
BLOB_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"


def _raise_offline(encoding_name: str) -> None:
    raise ConnectionError("offline probe")


@pytest.fixture(autouse=True)
def _reset_encoding_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例独立于其他用例留下的进程级「不可用」锁。"""
    monkeypatch.setattr(tokens_module, "_ENCODING_UNAVAILABLE", None)


@pytest.fixture
def real_encoding():
    """真实 cl100k_base 编码；环境（无缓存且断网）加载不了则整段跳过。"""
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception as error:  # noqa: BLE001 — 测试环境探测，非产品路径
        pytest.skip(f"tiktoken 编码在本环境不可加载: {error}")


def test_exact_counts_for_ui02_corpus(real_encoding) -> None:
    """中文 / emoji / 代码 / 工具 schema 全部精确计数（不是 chars/4 类低值）。"""
    for text, expected in CORPUS.values():
        assert estimate_tokens(text) == expected


def test_exact_counts_beat_chars4_undercount(real_encoding) -> None:
    """中文语料精确计数显著高于 chars/4（纠偏：chars/4 低估不是安全兜底）。"""
    text, expected = CORPUS["chinese"]
    assert expected > len(text) // 4


def test_bound_dominates_exact_for_all_corpus(real_encoding) -> None:
    """上界证明的语料校验：UTF-8 字节数 ≥ 精确 token 数（逐条）。"""
    for text, expected in CORPUS.values():
        assert len(text.encode("utf-8")) >= expected


def test_load_failure_returns_proven_upper_bound_and_latches(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []

    def _offline(encoding_name: str) -> None:
        calls.append(encoding_name)
        raise ConnectionError("offline probe")

    monkeypatch.setattr(tiktoken, "get_encoding", _offline)

    with caplog.at_level(logging.WARNING, logger=_STORE_LOGGER):
        first = estimate_tokens("你好")
        second = estimate_tokens("你好")

    assert first == len("你好".encode()) == 6
    assert second == 6
    # 进程内锁定：失败只触发一次，不重复走下载重试
    assert calls == ["cl100k_base"]
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    message = caplog.records[0].getMessage()
    assert "ConnectionError" in message
    assert "TIKTOKEN_CACHE_DIR" in message


def test_first_boot_offline_empty_cache_dir_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """首启断网（票面 AC）：空缓存目录 + 断网 → 上界回退，异常不裸逃逸。"""
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "empty-cache"))
    # 强制重建：registry 记忆化的编码会让本用例空转
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})

    def _offline(blobpath: str) -> None:
        raise ConnectionError(f"network unreachable: {blobpath}")

    monkeypatch.setattr(tiktoken.load, "read_file", _offline)

    with caplog.at_level(logging.WARNING, logger=_STORE_LOGGER):
        assert estimate_tokens("你好") == 6  # UTF-8 字节数上界
    assert caplog.records


def test_prewarmed_verified_cache_gives_exact_counts_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_encoding
) -> None:
    """预置校验过的离线缓存（票面首选路径）：零网络访问拿到精确计数。"""
    try:
        blob = tiktoken.load.read_file_cached(BLOB_URL, BLOB_SHA256)
    except Exception as error:  # noqa: BLE001 — 测试环境取料失败即跳过
        pytest.skip(f"无法取得 cl100k blob（默认缓存与网络皆不可用）: {error}")

    cache_dir = tmp_path / "prewarmed"
    cache_dir.mkdir()
    (cache_dir / hashlib.sha1(BLOB_URL.encode()).hexdigest()).write_bytes(blob)

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})
    # 缓存命中即加载成功：read_file（真下载路径）被触达即失败
    monkeypatch.setattr(
        tiktoken.load,
        "read_file",
        lambda _blobpath: pytest.fail("预置缓存命中后不得访问网络"),
    )

    for text, expected in CORPUS.values():
        assert estimate_tokens(text) == expected


def test_corrupt_cache_offline_does_not_fabricate_low_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """损坏缓存 + 断网：明确不可用 → 保守上界，不虚构低值继续发送。"""
    cache_dir = tmp_path / "corrupt-cache"
    cache_dir.mkdir()
    (cache_dir / hashlib.sha1(BLOB_URL.encode()).hexdigest()).write_bytes(b"garbage")

    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(tiktoken.registry, "ENCODINGS", {})

    def _offline(blobpath: str) -> None:
        raise ConnectionError(f"network unreachable: {blobpath}")

    monkeypatch.setattr(tiktoken.load, "read_file", _offline)

    with caplog.at_level(logging.WARNING, logger=_STORE_LOGGER):
        assert estimate_tokens("你好") == 6  # 上界，不是编造的「精确值」
    assert any("加载失败" in record.getMessage() for record in caplog.records)


def test_message_estimation_stays_bounded_under_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """消息级估算共用同一回退：整列表仍是逐消息上界之和（不裸逃逸）。"""
    monkeypatch.setattr(tiktoken, "get_encoding", _raise_offline)
    message = HumanMessage(content="你好，世界")
    assert estimate_message_tokens([message]) == len(
        message.model_dump_json().encode("utf-8")
    )


def test_concurrent_first_failure_warns_exactly_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """latch 竞态钉：并发首用失败恰好一条 WARNING（P3 残余：无锁双告警）。

    无锁时 N 个线程都通过 `_ENCODING_UNAVAILABLE is None` 检查、各自失败各自
    告警——同一次断网被报 N 次。修复后 check-set-warn 在锁内，恰好一次；
    所有调用仍拿到上界回退值（行为不变，只是告警去重）。
    """
    import time
    from concurrent.futures import ThreadPoolExecutor

    def _slow_fail(name: str):
        time.sleep(0.05)  # 撑开检查-设置窗口，让无锁实现必然双告警
        raise ConnectionError("offline race probe")

    monkeypatch.setattr(tiktoken, "get_encoding", _slow_fail)

    with caplog.at_level(logging.WARNING, logger=_STORE_LOGGER), ThreadPoolExecutor(
        max_workers=8
    ) as pool:
        results = list(pool.map(lambda _: estimate_tokens("text"), range(8)))

    assert results == [len(b"text")] * 8
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"并发首用失败必须恰好一条 WARNING，实得 {len(warnings)}"
