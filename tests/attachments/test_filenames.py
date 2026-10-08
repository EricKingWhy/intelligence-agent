"""`attachments.filenames.file_leaf_name` 单元测试（来源: DSH `fileLeafName`）。"""

from __future__ import annotations

from agent_harness.attachments.filenames import file_leaf_name


def test_strips_posix_and_windows_paths() -> None:
    assert file_leaf_name("/home/user/pictures/cat.png") == "cat.png"
    # POSIX 宿主把 `\` 当普通字符——必须手工剥掉，否则会把 Windows 完整路径泄漏出去。
    assert file_leaf_name(r"C:\Users\me\secret\dog.jpg") == "dog.jpg"


def test_control_and_illegal_characters_are_neutralized() -> None:
    assert file_leaf_name("a\x00b\x1fc.png") == "abc.png"
    assert file_leaf_name('we<ird>:"|?*.png') == "we_ird______.png"


def test_trailing_dots_and_spaces_are_stripped() -> None:
    assert file_leaf_name("name.png.  ") == "name.png"


def test_windows_device_names_are_prefixed() -> None:
    assert file_leaf_name("CON") == "_CON"
    assert file_leaf_name("nul.png") == "_nul.png"
    assert file_leaf_name("COM1") == "_COM1"


def test_utf8_byte_length_is_capped_at_255() -> None:
    # 每个中文字符 3 字节；1024 个字符远超 255 字节 ⇒ 截断到 ≤255 字节。
    result = file_leaf_name("图" * 1024 + ".png")
    assert len(result.encode("utf-8")) <= 255
    assert result.startswith("图")


def test_empty_or_dot_names_fall_back() -> None:
    assert file_leaf_name(None) == "file"
    assert file_leaf_name("") == "file"
    assert file_leaf_name(".") == "file"
    assert file_leaf_name("..") == "file"
    assert file_leaf_name("/") == "file"
