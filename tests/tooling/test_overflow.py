"""Artifact overflow 的公开边界与完整输出恢复。"""

import json

import pytest

from agent_harness.storage.artifact import FakeArtifactStore
from agent_harness.tooling import ToolResult
from agent_harness.tooling.overflow import (
    ArtifactOverflowHandler,
    ArtifactOverflowUnavailable,
)
from tests.conftest import make_session


@pytest.mark.asyncio
async def test_small_output_returns_original_result(tmp_path):
    session = make_session(tmp_path)
    before = session.events
    result = ToolResult.success("ok", data={"output": "x" * 2000})
    handler = ArtifactOverflowHandler(FakeArtifactStore())
    compact, deferred = await handler.maybe_overflow(session, "call", "read", result)
    assert compact is result and deferred == []
    assert session.events == before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["output", "content", "stdout", "stderr", "message"])
async def test_large_output_saved_before_summary_and_event(tmp_path, field):
    session = make_session(tmp_path)
    store = FakeArtifactStore()
    raw = "\n".join(f"line {i}: 中文" for i in range(5000))
    result = (ToolResult.success(raw) if field == "message" else
              ToolResult.success("ok", data={field: raw, "exit_code": 1}))
    before = result.model_dump()
    compact, deferred = await ArtifactOverflowHandler(store).maybe_overflow(
        session, "call", "bash", result,
    )
    artifact = await store.load(compact.artifact_ref)
    assert artifact.content == raw
    summary = compact.message if field == "message" else compact.data[field]
    assert len(summary) < 2000
    assert "line 0:" in summary and "line 4999:" in summary
    assert f"use read_artifact({artifact.artifact_id})" in summary
    assert result.model_dump() == before
    # R6-7 契约：handler 不再直接 append——事件以 (type, data) 形式返回，
    # 由 Runtime 在 tool/call 落盘之后追加（消除事件日志前向引用）。
    assert deferred == [(
        "artifact/externalized",
        {"artifact_id": artifact.artifact_id, "session_id": session.session_id,
         "source_tool": "bash", "tool_call_id": "call",
         "size": artifact.size, "mime_type": "text/plain"},
    )]
    assert not any(e.type == "artifact/externalized" for e in session.events)


@pytest.mark.asyncio
async def test_both_streams_and_duplicate_message_are_preserved_and_bounded(tmp_path):
    session = make_session(tmp_path)
    store = FakeArtifactStore()
    stdout, stderr = "a" * 10000, "b" * 10000
    result = ToolResult.success(stdout, data={"stdout": stdout, "stderr": stderr})
    compact, _deferred = await ArtifactOverflowHandler(store).maybe_overflow(
        session, "call", "bash", result,
    )
    artifact = await store.load(compact.artifact_ref)
    assert json.loads(artifact.content) == {
        "stdout": stdout, "stderr": stderr, "message": stdout,
    }
    assert artifact.mime_type == "application/json"
    assert len(compact.message) <= 2000
    assert all(len(value) <= 2000 for value in compact.data.values())


@pytest.mark.asyncio
async def test_upload_failure_can_fail_closed_for_audit_paths(tmp_path):
    class UnavailableStore(FakeArtifactStore):
        async def save(self, *args, **kwargs):
            raise ConnectionError("unavailable")

    session = make_session(tmp_path)
    result = ToolResult.success("x" * 5000)
    with pytest.raises(ArtifactOverflowUnavailable, match="Artifact store failed"):
        await ArtifactOverflowHandler(
            UnavailableStore(), fail_open=False
        ).maybe_overflow(session, "call", "git_status", result)


@pytest.mark.asyncio
async def test_missing_store_can_fail_closed_for_audit_paths(tmp_path):
    session = make_session(tmp_path)
    result = ToolResult.success("x" * 5000)
    with pytest.raises(ArtifactOverflowUnavailable, match="Artifact store is required"):
        await ArtifactOverflowHandler(
            None, fail_open=False
        ).maybe_overflow(session, "call", "git_status", result)


@pytest.mark.asyncio
async def test_upload_failure_fails_open_with_raw_result(tmp_path):
    """T5 (#135): store unavailable → fail-open: keep raw tool result in-session."""

    class UnavailableStore(FakeArtifactStore):
        async def save(self, *args, **kwargs):
            raise ConnectionError("unavailable")

    session = make_session(tmp_path)
    before = session.events
    result = ToolResult.success("x" * 5000)
    compact, deferred = await ArtifactOverflowHandler(
        UnavailableStore()
    ).maybe_overflow(session, "call", "bash", result)
    # Fail-open: no externalization, original result returned unchanged.
    assert compact is result
    assert deferred == []
    assert result.artifact_ref is None
    assert session.events == before


def test_overflow_chars_smaller_than_marker_is_rejected_at_construction():
    """overflow_chars 小于截断 marker 最小长度时构造期快速失败。

    marker（≈110+ 字符，含 artifact_id）本身不受 head/tail 预算约束：
    overflow_chars 太小时摘要必然超出预算、悄悄污染 Context——与其运行时
    静默违约，不如配置期直接拒绝。默认预算 2000 必须仍可正常构造。"""
    with pytest.raises(ValueError, match="overflow_chars"):
        ArtifactOverflowHandler(FakeArtifactStore(), overflow_chars=50)
    # 默认预算不受影响（既有行为回归锚点）。
    ArtifactOverflowHandler(FakeArtifactStore())


@pytest.mark.asyncio
async def test_diff_view_fields_are_overflow_covered(tmp_path):
    """write/edit 的 before/after diff 字段（Q12=B 前端双栏视图原料）也在
    overflow 预算内：写大文件时完整内容进 Artifact、模型只拿摘要。此前只
    覆盖 output/content/stdout/stderr——读大文件被摘要，写大文件却把
    ~2×50KB 原文回灌进 Context（不变量 #15：模型只拿 summary + ref）。"""
    session = make_session(tmp_path)
    events_before = list(session.events)
    store = FakeArtifactStore()
    big = "\n".join(f"line {i}: 内容" for i in range(5000))
    result = ToolResult.success(
        "已写入。",
        data={"path": "x.txt", "before": "", "after": big, "truncated": False},
    )
    compact, deferred = await ArtifactOverflowHandler(store).maybe_overflow(
        session, "call", "write", result,
    )
    artifact = await store.load(compact.artifact_ref)
    assert artifact.content == big
    assert len(compact.data["after"]) < 2000
    assert "read_artifact" in compact.data["after"]
    assert compact.data["before"] == ""
    assert compact.data["path"] == "x.txt" and compact.data["truncated"] is False
    assert deferred and deferred[0][0] == "artifact/externalized"
    assert session.events == events_before, "延迟事件由 Runtime 在 tool/call 后追加，handler 不落盘"


@pytest.mark.asyncio
async def test_local_store_writes_to_disk_and_reads_back(tmp_path):
    """外置链路落到**本地 Provider**（#192）：阈值 → 磁盘 → 按 ref 读回全量。

    用真实文件系统而不是 Fake：本地 Provider 是未配对象存储时的默认路径，
    而它此前根本不存在——所以"产物真的落了盘、并且能读回来"这件事必须被
    真文件系统证明一次（原子写、键形态、读回切片都在真盘上）。
    """
    from agent_harness.config import Settings
    from agent_harness.storage.local_artifact import LocalArtifactStore

    session = make_session(tmp_path)
    settings = Settings(_env_file=None, artifact_dir=str(tmp_path / "artifacts"))
    store = LocalArtifactStore(settings, session_id=session.session_id)
    raw = "\n".join(f"line {i}" for i in range(3000))
    result = ToolResult.success("ok", data={"stdout": raw, "exit_code": 0})

    compact, deferred = await ArtifactOverflowHandler(store).maybe_overflow(
        session, "call-1", "bash", result,
    )

    artifact_id = compact.artifact_ref
    assert artifact_id is not None
    # 落盘形态：<artifact_dir>/<session_id>/<artifact_id>
    on_disk = tmp_path / "artifacts" / session.session_id / artifact_id
    assert on_disk.read_text(encoding="utf-8") == raw
    assert deferred[0][0] == "artifact/externalized"
    # 模型侧读回：全量可分片取回（这里取尾部一行证明不是只读了摘要）
    slice_ = await store.inspect(artifact_id, start_line=3000)
    assert slice_.lines[0]["text"] == "line 2999"
    assert slice_.total_lines == 3000


@pytest.mark.asyncio
@pytest.mark.parametrize("read_tool_name", ["read_artifact", "inspect_artifact"])
async def test_summary_names_the_paired_read_tool(tmp_path, read_tool_name):
    """#186 AC4：摘要里的读回提示必须点名**与本 store 配对**的工具。

    `storage/artifact_select.py` 的配对表是 S3 → `inspect_artifact`、
    MinIO / Local → `read_artifact`。此前这里写死 `read_artifact`，于是 S3 部署的
    提示会把模型指向一个没有注册的工具名；前端也按同一段文字提取 artifact_id，
    名字对不上时界面就永远看不到归档内容（`DiffBlock` 的归档态成了死路径）。
    """
    session = make_session(tmp_path)
    store = FakeArtifactStore()
    raw = "\n".join(f"line {i}" for i in range(5000))
    handler = ArtifactOverflowHandler(store, read_tool_name=read_tool_name)

    compact, _ = await handler.maybe_overflow(
        session, "call", "bash", ToolResult.success("ok", data={"stdout": raw}),
    )

    artifact = await store.load(compact.artifact_ref)
    summary = compact.data["stdout"]
    assert f"use {read_tool_name}({artifact.artifact_id})" in summary
    # 另一个名字不得出现——两个名字都写进去等于没说清该调哪个
    other = "inspect_artifact" if read_tool_name == "read_artifact" else "read_artifact"
    assert other not in summary


# ---------------------------------------------------------------------------
# #644（T15b/O-1 + T8e）：结构化载荷与非白名单字段绕过溢出预算
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"payload": "x" * 50000},                        # 嵌套 dict（票面原始形态）
    ["y" * 30000, {"z": "w" * 20000}],               # 嵌套 list/dict 混合
])
async def test_structured_whitelist_field_is_externalized_as_restorable_json(
    tmp_path, payload,
):
    """T15b/O-1（#644）：白名单字段装大结构化载荷时必须外置且可完整还原。

    此前 oversized 只认 ``isinstance(value, str)``，``data.output`` 装 50KB dict
    直接穿透回灌 Context（不变量 #15：模型只拿 summary + ref）。修复后按紧凑
    JSON 序列化大小参与判定，artifact 保存可还原 JSON，模型侧只见摘要。"""
    session = make_session(tmp_path)
    store = FakeArtifactStore()
    result = ToolResult.success("ok", data={"output": payload})
    before = result.model_dump()
    compact, deferred = await ArtifactOverflowHandler(store).maybe_overflow(
        session, "call", "read", result,
    )
    assert compact.artifact_ref
    artifact = await store.load(compact.artifact_ref)
    assert json.loads(artifact.content) == payload        # load 后还原原始对象
    assert artifact.mime_type == "application/json"
    assert len(compact.data["output"]) < 2000             # 模型侧不再携带原文
    full = json.dumps(payload, ensure_ascii=False)
    assert full not in json.dumps(compact.data, ensure_ascii=False)
    assert compact.ok                                     # 结果语义不变
    assert deferred and deferred[0][0] == "artifact/externalized"
    assert not any(e.type == "artifact/externalized" for e in session.events)
    assert result.model_dump() == before                  # 原 result 不被就地修改


@pytest.mark.asyncio
async def test_non_allowlist_large_string_is_externalized(tmp_path):
    """T8e（#644）：非白名单顶层大字符串此前没有任何预算。

    data 的非白名单顶层字段（``text``）统一按序列化大小参与判定；替换字段
    指向可回读 artifact，白名单语义与其它小字段原样保留。"""
    session = make_session(tmp_path)
    store = FakeArtifactStore()
    raw = "x" * 50000
    result = ToolResult.success("ok", data={"text": raw, "exit_code": 0})
    before = result.model_dump()
    compact, deferred = await ArtifactOverflowHandler(store).maybe_overflow(
        session, "call", "read", result,
    )
    assert compact.artifact_ref
    artifact = await store.load(compact.artifact_ref)
    assert artifact.content == raw                        # 原文完整可还原
    assert len(compact.data["text"]) < 2000               # 模型侧只见摘要
    assert compact.data["exit_code"] == 0                 # 小字段原样保留
    assert all(not isinstance(value, str) or len(value) <= 2000
               for value in compact.data.values())
    assert compact.ok
    assert deferred and deferred[0][0] == "artifact/externalized"
    assert result.model_dump() == before


@pytest.mark.asyncio
async def test_threshold_equality_stays_strict_for_new_entries(tmp_path):
    """AC3：既有严格比较符（> 阈值才外置）对新增两类入口同样成立。

    紧凑 JSON 序列化长度恰等于 overflow_chars 的 dict 与恰等于阈值的非白名单
    字符串都保持原样；各加 1 字符才触发外置；嵌套 list/dict 由序列化大小覆盖。"""
    session = make_session(tmp_path)
    store = FakeArtifactStore()
    pad = "x" * (2000 - len(json.dumps({"p": ""})))       # 序列化后恰 2000 字符
    at_limit = ToolResult.success("ok", data={"output": {"p": pad}})
    compact, deferred = await ArtifactOverflowHandler(store).maybe_overflow(
        session, "call", "read", at_limit,
    )
    assert compact is at_limit and deferred == []
    text_at_limit = ToolResult.success("ok", data={"text": "x" * 2000})
    compact, deferred = await ArtifactOverflowHandler(store).maybe_overflow(
        session, "call", "read", text_at_limit,
    )
    assert compact is text_at_limit and deferred == []
    over = ToolResult.success("ok", data={"text": "x" * 2001})
    compact, deferred = await ArtifactOverflowHandler(store).maybe_overflow(
        session, "call", "read", over,
    )
    assert compact.artifact_ref and deferred


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    {"output": {"payload": "x" * 50000}},                # T15b/O-1 入口
    {"text": "x" * 50000},                               # T8e 入口
])
async def test_new_entries_preserve_fail_open_and_fail_closed(tmp_path, data):
    """AC4：新增两类入口沿用既有 fail-open（store 缺失/上传失败保原文返回）
    与 fail-closed（审计路径抛 ArtifactOverflowUnavailable）契约，不伪造 ref。"""
    session = make_session(tmp_path)

    class UnavailableStore(FakeArtifactStore):
        async def save(self, *args, **kwargs):
            raise ConnectionError("unavailable")

    result = ToolResult.success("ok", data=data)
    compact, deferred = await ArtifactOverflowHandler(
        UnavailableStore(),
    ).maybe_overflow(session, "call", "read", result)
    assert compact is result and deferred == []           # fail-open：原文保留
    assert compact.artifact_ref is None

    with pytest.raises(ArtifactOverflowUnavailable, match="Artifact store failed"):
        await ArtifactOverflowHandler(
            UnavailableStore(), fail_open=False,
        ).maybe_overflow(session, "call", "read", ToolResult.success("ok", data=data))
    with pytest.raises(ArtifactOverflowUnavailable, match="Artifact store is required"):
        await ArtifactOverflowHandler(
            None, fail_open=False,
        ).maybe_overflow(session, "call", "read", ToolResult.success("ok", data=data))
