"""`#828` / MM-07：CLI `--image <path>` —— 附图命令行的接纳、上传与错误语义。

票面验收映射（PRD `#821` D10）：

- AC1 `--image <path>` 可重复；
- AC2 读文件 → magic-bytes 探测 MIME → 落到本会话附件存储 → `user/message` 事件带引用；
- AC3 三条失败路径（路径不存在 / 非受支持格式 / 超限）都是**明确错误 + 非零退出 +
  零残留**（不发消息、不留附件字节）；
- AC4 模型不支持视觉 ⇒ 提交前拒；
- AC5 纯文本调用行为逐字不变（无 `attachments` 键）。

断言口径：只看外部可观察行为——`user/message` 事件里的引用数组、字节能否从存储读回、
stderr 文案、退出码、有无残留文件/事件。不断言私有函数名与内部字段顺序。

审查修复批（`#828` 双轴 13 findings）另加：

- 声明闸门：`shot.jpg` 装 PNG 字节 ⇒ 拒（与 Web 上传端点同一份判定，A-F1）；
- 有界读：超大文件不走 `Path.read_bytes()` 全量读入（A-F2）；
- 像素 / 边长按各自原因归类，不笼统说成"不是受支持的图片"（A-F3）；
- 目录 / 非常规文件给准确文案（A-F6）；
- 两条退出通道：输入不成立 exit 1、命令行形状错 exit 2（B-P3-3）。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_harness import cli
from agent_harness.config import Settings
from agent_harness.session import USER_MESSAGE, JsonlSessionStore
from agent_harness.storage.artifact_select import select_artifact_store
from tests.attachments.helpers import large_png_bytes, png_bytes
from tests.scripted_model import ScriptedModel

# ── 测试装置 ────────────────────────────────────────────────────────────


def _settings(tmp_path: Path, **overrides) -> Settings:
    """自足 Settings：`mimo` preset 声明 `supports_vision=True`（成功路径的前提）。

    `_env_file=None` 不吃仓库根 `.env`；`artifact_dir` 指到 tmp_path —— 默认值
    `.agent/artifacts` 是**相对 CWD** 的，不给它就没法在 tmp 里断言"有无残留"。
    """
    base: dict = {
        "model_api_key": "sk-test",
        "model_provider": "mimo",
        "model_name": "mimo-v2.6-flash",
        "workspace_dir": str(tmp_path / "workspace"),
        "artifact_dir": str(tmp_path / "artifacts"),
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def _sessions_root(settings: Settings) -> Path:
    return Path(settings.workspace_dir) / "sessions"


def _session_ids(settings: Settings) -> list[str]:
    """磁盘上真有事件的会话 id（布局 `<root>/<session_id>/events.jsonl`）。"""
    root = _sessions_root(settings)
    if not root.exists():
        return []
    return sorted(path.parent.name for path in root.glob("*/events.jsonl"))


def _artifact_files(settings: Settings) -> list[Path]:
    root = Path(settings.artifact_dir)
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def _events(settings: Settings, session_id: str):
    return JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)


def _user_events(settings: Settings):
    session_ids = _session_ids(settings)
    assert len(session_ids) == 1, f"应有且仅有一个会话，实际 {session_ids}"
    events = [e for e in _events(settings, session_ids[0]) if e.type == USER_MESSAGE]
    assert len(events) == 1, f"应恰好一条 user/message，实际 {len(events)} 条"
    return session_ids[0], events[0]


def _scripted(monkeypatch, model: ScriptedModel) -> ScriptedModel:
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model", lambda config, **kw: model
    )
    return model


def _run_cli_rejected(monkeypatch, capsys, settings, images) -> str:
    """经 `_main_dispatch` 跑一次带图 CLI，断言「exit 1 + 零残留 + 模型未被构造」。

    返回 stderr 供文案断言。`create_chat_model` 被换成"调了就炸"的探针：被拒的 run
    必须拒在**构造模型之前**（提交前拒的机械证据）。
    """

    def _forbid(config, **kw):
        raise AssertionError("被拒的 CLI 不该走到构造模型那一步")

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr("agent_harness.assembly.create_chat_model", _forbid)
    argv = ["agent-harness", "看这张图"]
    for image in images:
        argv += ["--image", str(image)]
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exit_info:
        cli._main_dispatch()

    # 两条退出通道的分工（B-P3-3）：输入不成立 ⇒ exit 1；命令行形状错 ⇒ argparse 的 2。
    # 逐字锚 1（外部契约），再确认常量没被改值后与实际退出码脱节。
    assert cli.IMAGE_INPUT_EXIT_CODE == 1, "输入不成立这一档的退出码契约必须是 1"
    assert exit_info.value.code == cli.IMAGE_INPUT_EXIT_CODE
    stderr = capsys.readouterr().err
    assert _session_ids(settings) == [], "被拒的 run 不得留下任何会话事件"
    assert _artifact_files(settings) == [], "被拒的 run 不得留下附件字节"
    return stderr


# ── AC1：旗标形状 ───────────────────────────────────────────────────────


def test_image_flag_is_repeatable_and_reaches_run(monkeypatch, tmp_path):
    """`--image` 可重复；单次给也是**列表**（append 语义，不是裸字符串）。"""
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    captured: list[dict] = []

    async def fake_run(message, **kwargs):
        captured.append({"message": message, **kwargs})
        return cli.RunOutcome(final_text="ok")

    monkeypatch.setattr(cli, "run", fake_run)

    monkeypatch.setattr(
        sys, "argv", ["agent-harness", "看图", "--image", "a.png", "--image", "b.png"]
    )
    cli._main_dispatch()
    assert captured[-1]["images"] == ["a.png", "b.png"]
    assert captured[-1]["message"] == "看图"

    monkeypatch.setattr(sys, "argv", ["agent-harness", "看图", "--image", "a.png"])
    cli._main_dispatch()
    assert captured[-1]["images"] == ["a.png"]

    monkeypatch.setattr(sys, "argv", ["agent-harness", "看图"])
    cli._main_dispatch()
    assert captured[-1]["images"] is None, "不给 --image ⇒ None（纯文本路径零改动）"


# ── AC2：成功路径 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("filename", ["shot.png", "shot.bin"])
@pytest.mark.asyncio
async def test_image_is_stored_and_referenced_in_the_user_message(
    monkeypatch, tmp_path, filename
):
    """有效图片 ⇒ 字节落存储 + 事件引用可读回 + 模型真收到图片内容块。

    `.bin` 取名是刻意的：类型判定必须是**字节签名**，不是扩展名。
    """
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    model = _scripted(monkeypatch, ScriptedModel([AIMessage(content="看到了")]))
    payload = png_bytes(4, 3)
    image_path = tmp_path / filename
    image_path.write_bytes(payload)

    printed: list[str] = []
    outcome = await cli.run("这张图里有什么", images=[str(image_path)], write=printed.append)

    assert outcome.paused is False
    assert outcome.final_text == "看到了"
    session_id, event = _user_events(settings)
    attachment_id = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert event.data["attachments"] == [
        {
            "kind": "image",
            "attachment_id": attachment_id,
            "media_type": "image/png",
            "bytes": len(payload),
            "width": 4,
            "height": 3,
        }
    ]
    # 先落存储后写事件：事件里的引用必须能按同一 id 读回同一份字节。
    selection = select_artifact_store(settings, session_id)
    assert selection is not None
    blob = await selection.store.load_bytes(attachment_id)
    assert blob.content == payload
    assert blob.mime_type == "image/png"
    # MM-02 物化链在 CLI 上也通：provider 载荷里恰好一个内联图片块。媒体类型锚
    # `data:image/` 而不锚 png —— 载荷是**归一化之后**的字节（MM-02 D11 的目标格式），
    # 事件引用里的 `media_type` 才是源字节的事实（上面那条断言）。
    blocks = [
        block
        for snapshot in model.snapshots
        for message in snapshot.messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]
    assert len(blocks) == 1
    url = blocks[0]["image_url"]["url"]
    assert url.startswith("data:image/")
    assert len(url.partition("base64,")[2]) > 64


@pytest.mark.asyncio
async def test_repeated_path_references_one_attachment(monkeypatch, tmp_path):
    """同一份字节给两次 ⇒ 一个引用（与发送端点按 attachment_id 去重的口径一致）。"""
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    _scripted(monkeypatch, ScriptedModel([AIMessage(content="好")]))
    image_path = tmp_path / "shot.png"
    image_path.write_bytes(png_bytes(4, 3))

    await cli.run("看图", images=[str(image_path), str(image_path)], write=[].append)

    _, event = _user_events(settings)
    assert len(event.data["attachments"]) == 1


# ── AC5：纯文本路径逐字不变 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_only_run_records_no_attachments_key(monkeypatch, tmp_path):
    """不给 `--image` ⇒ `user/message` 事件里连 `attachments` 键都没有。"""
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    _scripted(monkeypatch, ScriptedModel([AIMessage(content="好")]))

    outcome = await cli.run("纯文本任务", write=[].append)

    assert outcome.final_text == "好"
    _, event = _user_events(settings)
    assert "attachments" not in event.data


# ── AC3：三条失败路径（明确错误 + 非零退出 + 零残留） ───────────────────


def test_missing_image_path_is_rejected(monkeypatch, capsys, tmp_path):
    settings = _settings(tmp_path)
    missing = tmp_path / "nope.png"

    stderr = _run_cli_rejected(monkeypatch, capsys, settings, [missing])

    assert str(missing) in stderr, "错误文案必须点名具体路径"
    assert "不存在" in stderr


def test_non_image_bytes_are_rejected(monkeypatch, capsys, tmp_path):
    settings = _settings(tmp_path)
    fake = tmp_path / "note.png"
    fake.write_bytes("这不是一张图片，只是文本。".encode())

    stderr = _run_cli_rejected(monkeypatch, capsys, settings, [fake])

    assert str(fake) in stderr
    assert "图片" in stderr


def test_image_over_single_byte_limit_is_rejected(monkeypatch, capsys, tmp_path):
    settings = _settings(tmp_path, attachment_max_image_bytes=64)
    image = tmp_path / "big.png"
    image.write_bytes(large_png_bytes(1, 1, payload_bytes=4096))

    stderr = _run_cli_rejected(monkeypatch, capsys, settings, [image])

    assert str(image) in stderr
    assert "单张字节上限" in stderr


def test_oversized_image_is_not_read_wholesale(monkeypatch, capsys, tmp_path):
    """A-F2：单张读取按上限 + 1 字节即停——绝不 `Path.read_bytes()` 整个读进来。"""
    settings = _settings(tmp_path, attachment_max_image_bytes=64)
    image = tmp_path / "huge.png"
    image.write_bytes(png_bytes(2, 2) + b"\x00" * (8 * 1024 * 1024))
    assert image.stat().st_size > 8 * 1024 * 1024

    read_calls: list[Path] = []
    original = Path.read_bytes

    def spy(self: Path) -> bytes:
        read_calls.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    stderr = _run_cli_rejected(monkeypatch, capsys, settings, [image])

    assert "单张字节上限" in stderr
    assert image not in read_calls, "超大文件不得走 Path.read_bytes 全量读入"


def test_image_over_pixel_limit_is_rejected(monkeypatch, capsys, tmp_path):
    """A-F3：像素超限按自己的原因归类，不再笼统说成"不是受支持的图片"。"""
    settings = _settings(tmp_path, attachment_max_image_pixels=4)
    image = tmp_path / "many.png"
    image.write_bytes(png_bytes(4, 3))  # 12 像素 > 4

    stderr = _run_cli_rejected(monkeypatch, capsys, settings, [image])

    assert str(image) in stderr
    assert "像素" in stderr
    assert "不是受支持的图片" not in stderr, "归类词不能错位"


def test_image_over_dimension_limit_is_rejected(monkeypatch, capsys, tmp_path):
    settings = _settings(tmp_path, attachment_max_image_dimension=8)
    image = tmp_path / "wide.png"
    image.write_bytes(png_bytes(16, 4))

    stderr = _run_cli_rejected(monkeypatch, capsys, settings, [image])

    assert str(image) in stderr
    assert "边长超过上限" in stderr


def test_extension_mismatch_is_rejected(monkeypatch, capsys, tmp_path):
    """A-F1：`shot.jpg` 装 PNG 字节 ⇒ 与 Web 上传端点同判（扩展名声明不符即拒）。

    不比对时这条输入会被静默接受成 `image/png`，而同一文件经 Web 上传端点必拒
    （`IMAGE_TYPE_MISMATCH`）——同一份输入两个入口结论相反。
    """
    settings = _settings(tmp_path)
    image = tmp_path / "shot.jpg"
    image.write_bytes(png_bytes(4, 3))

    stderr = _run_cli_rejected(monkeypatch, capsys, settings, [image])

    assert str(image) in stderr
    assert "不符" in stderr and "扩展名" in stderr


def test_directory_path_is_reported_as_not_a_regular_file(monkeypatch, capsys, tmp_path):
    """A-F6：路径存在但不是常规文件 ⇒ 准确文案，不谎报"不存在"。"""
    settings = _settings(tmp_path)
    directory = tmp_path / "a-directory"
    directory.mkdir()

    stderr = _run_cli_rejected(monkeypatch, capsys, settings, [directory])

    assert str(directory) in stderr
    assert "不是常规文件" in stderr
    assert "不存在" not in stderr


def test_too_many_images_are_rejected(monkeypatch, capsys, tmp_path):
    settings = _settings(tmp_path, attachment_max_images_per_message=1)
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    first.write_bytes(png_bytes(2, 2))
    second.write_bytes(png_bytes(3, 3))

    stderr = _run_cli_rejected(monkeypatch, capsys, settings, [first, second])

    assert "最多 1 张" in stderr
    assert "2" in stderr


def test_argument_usage_error_exits_2(monkeypatch, capsys):
    """B-P3-3：命令行**形状**不对走 argparse 的 exit 2（与 `--image` 的 exit 1 分流）。"""
    monkeypatch.setattr(sys, "argv", ["agent-harness"])  # 缺 message 位置参数

    with pytest.raises(SystemExit) as exit_info:
        cli._main_dispatch()

    assert exit_info.value.code == 2


# ── AC4：模型不支持视觉 ⇒ 提交前拒 ──────────────────────────────────────


def test_model_without_vision_rejects_images(monkeypatch, capsys, tmp_path):
    """`deepseek` preset 未声明 `supports_vision`（省略 ⇒ False，不猜）⇒ 拒。"""
    settings = _settings(tmp_path, model_provider="deepseek", model_name="deepseek-chat")
    image = tmp_path / "shot.png"
    image.write_bytes(png_bytes(4, 3))

    stderr = _run_cli_rejected(monkeypatch, capsys, settings, [image])

    assert "supports_vision" in stderr
    assert "--image" in stderr
