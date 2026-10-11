"""#939 M-27：`ImageRef.name` 的**上游来源**是否真的接上。

审计（MM A-F20 复核）纠正过一处事实：上传端点**确实**返回 `name`
（`web/attachments.py` 用 `file_leaf_name` 回填），缺的是**从上传回执到事件的回传信道**。
按入口逐条查上游到底产不产 name：

- **CLI `--image <path>`**：文件名就在手上 ⇒ **产得出**，此前被丢掉（本票接上）；
- **Web 发送端点**：只收 `attachment_ids`（`SendMessageRequest.attachments` 是
  `list[str]`），没有信道 ⇒ 前端若要传名字需**改发送请求契约**（`web/src/lib/api.ts`
  的 `SendMessageRequest` 与 `app.py` 的 pydantic 模型）——票据边界之外，属 MM-03。

故"恒缺省"的准确表述是：**能产的上游此前没接、没信道的那条如实缺省**。
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image


def _png(path: Path, *, size: tuple[int, int] = (8, 6)) -> Path:
    buffer = io.BytesIO()
    Image.new("RGB", size, (12, 34, 56)).save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())
    return path


def test_cli_image_references_carry_the_display_name(tmp_path: Path) -> None:
    """CLI `--image shots/cat.png` → 事件引用带 `name="cat.png"`（去路径，不带目录）。"""
    (tmp_path / "shots").mkdir(parents=True, exist_ok=True)
    shot = _png(tmp_path / "shots" / "cat.png")
    refs = _admit_cli_images_for_test(tmp_path, [shot])
    assert [ref["name"] for ref in refs] == ["cat.png"]


def _admit_cli_images_for_test(tmp_path: Path, paths: list[Path]) -> list[dict]:
    """`_admit_cli_images` 的最小调用封装（只喂它需要的 Settings + 模型目录）。"""
    import asyncio

    from agent_harness.cli import _admit_cli_images
    from agent_harness.config import Settings


    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        artifact_dir=str(tmp_path / "artifacts"),
        model_api_key="sk-test",
        # MM-03 视觉闸门在提交前拦非视觉模型（不另立口径）——这里只考展示名链路，
        # 故取 `mimo` preset（声明 supports_vision=True，与 tests/test_cli_image.py 同款）。
        model_provider="mimo",
        model_name="mimo-v2.6-flash",
    )
    return asyncio.run(
        _admit_cli_images(settings, "sess-m27", [str(p) for p in paths])
    )
