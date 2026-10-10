"""ArtifactStore：大输出的持久化边界（content-hash 寻址）。

为什么独立成 artifact.py：
- ToolResult 的主输出字段溢出时，原始内容存到这里，模型只拿截断摘要 + artifact_ref。
- 与 SessionEvent（对话历史）和 Operation Ledger（操作状态）不同种类的事实：
  SessionEvent 记"对话发生了什么"，Ledger 记"每次调用现在什么状态"，
  ArtifactStore 记"大输出本身存在哪、怎么找回"。

为什么 content-hash 寻址：
- 同一内容自动去重（两个 Tool 产出相同的 stdout 只存一份）；
- 寻址不需要额外 ID 生成器——hash 就是 ID；
- 跨 Session 理论上可共享（相同内容同 hash）。**文本** artifact 仍按 session 隔离 key
  （`{session_id}/{artifact_id}`）；**字节** artifact 自 #933 M-01 起走全局内容寻址根 +
  每会话回执（跨会话/fork 可寻址，归属由回执回答）。

物理位置：Runtime 域存储，不经过 Sandbox（spec 06 §3 + ADR-0006）。
默认 Provider：LocalArtifactStore（spec 06 §3：Local filesystem，开发/小型部署）。
远端 Provider：S3ArtifactStore（七牛云 Kodo S3 兼容）/ MinioArtifactStore。
测试 Provider：FakeArtifactStore（内存 dict）。
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod

from pydantic import BaseModel

#: artifact_id 的唯一形态：`sha256(content)[:16]`，16 位小写十六进制。
#: 两个远端 provider（S3 / MinIO）与 web 读接口共用这一份定义——此前 S3 内联正则、
#: MinIO 干脆不校验，两边行为不一致（#185 AC3）。
ARTIFACT_ID_PATTERN = re.compile(r"[0-9a-f]{16}")

#: **字节路径**的 artifact id 形态：完整内容哈希，带 `sha256:` 前缀
#: （`sha256:<64 hex>`）。与文本路径的 16 位短 id 是两个 id 空间：字节路径专供
#: 附件入站（#822 MM-01），不参与文本 artifact 的 overflow/inspect 语义。
#: 前缀是对外契约（附件响应 `attachment_id` 必须形如 `sha256:<hex>`），存储键用
#: 前缀后的 64 位 hex（路径安全，见各 Provider）。
BYTE_ARTIFACT_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")

#: artifact 存储键里 session 段的安全形态：**单个名字段，不是路径**。
#: 三个 Provider 的键都是 `{session_id}/{artifact_id}`；本地 Provider 要把它拼进文件
#: 系统路径，不校验时 `..` / 反斜杠段 / 盘符段都能越出 artifact 根目录（ADR-0029 D2
#: 的"路径穿越防护"同款问题）。
#: 规则本体放这里而不是 session 层：`session/service.py` 的 `_SESSION_ID_PATTERN`
#: 注释写的就是"字符集与 S3ArtifactStore 的 key 段规则一致"——同一条规则，一份定义。
#:
#: `{1,128}`（#517）：只有字符集白名单时，超长 id 会穿过校验直达文件系统——
#: Linux 上 `os.stat` 抛 `OSError [Errno 36] File name too long`（→ 裸 500），
#: Windows 上 `Path.exists()` 把超长名映射成 FileNotFoundError（→ 404），同一
#: 输入两种错误形态、都不是校验层的回答。128 与 uuid4 hex 生成形态同量级；
#: 白名单字符 + ≤128 长度保证拼出的路径必然在两个平台的文件名上限（255）内，
#: 结构上杜绝"通过校验的输入打穿到文件系统语义"。
SESSION_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


class Artifact(BaseModel):
    """一个 Artifact 的元数据 + 可选内容。"""

    artifact_id: str
    session_id: str
    size: int
    mime_type: str
    # 元数据"缺失即 None"，不得伪造成空串/默认值（#185 AC4）：MinIO 实现的 save 不
    # 持久化这三项，load 也无从恢复——填 "" 会让调用方以为"产物来自某工具，只是 id
    # 为空"。None 才是"本存储没存这一项"的如实表达。
    source_tool: str | None = None
    tool_call_id: str | None = None
    created_at: str | None = None
    content: str | None = None  # load() 时填充；inspect() 不填充


class ArtifactSlice(BaseModel):
    """inspect() 的局部读取结果。

    truncated 含义是"返回内容不完整"的并集：行数截断或字符截断任一发生即为 True。
    单行超长时该行按 max_chars 截断，原文始终完整保留在 Artifact 里，
    模型可凭 line_number 重新定位（spec 06 §4：大 Artifact 不完整灌回 Context）。
    """

    artifact_id: str
    lines: list[dict[str, int | str | bool]]
    total_lines: int
    returned_lines: int
    truncated: bool
    query: dict[str, int | str | None]


class BlobArtifact(BaseModel):
    """**字节路径**的 artifact：原始字节 + 内容寻址 id + Content-Type。

    与文本 `Artifact` 分开：文本路径的 `content` 是 `str`（UTF-8 假设），字节路径
    的 `content` 是 `bytes`。两者 key 布局与 id 形态都不同（16 位短 id vs
    `sha256:<64hex>`），是**两个 id 空间**（#822 MM-01）。
    """

    artifact_id: str
    session_id: str
    size: int
    mime_type: str
    content: bytes | None = None  # load_bytes() 时填充；save_bytes() 不填充


class ArtifactStore(ABC):
    """Artifact 的持久化边界（async ABC）。"""

    @abstractmethod
    async def save(
        self,
        session_id: str,
        content: str,
        *,
        mime_type: str,
        source_tool: str,
        tool_call_id: str,
    ) -> Artifact:
        """存内容，返回带 artifact_id (content-hash) 的 Artifact 元数据。"""

    @abstractmethod
    async def load(self, artifact_id: str) -> Artifact:
        """完整加载一个 Artifact（内容 + 元数据）。"""

    @abstractmethod
    async def inspect(
        self,
        artifact_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        keyword: str | None = None,
        max_lines: int = 200,
        max_chars_per_line: int = 2000,
    ) -> ArtifactSlice:
        """按行范围或关键词读取局部内容。

        max_chars_per_line 限制单行返回字符数，防止单条超长行灌爆 Context；
        截断行额外携带 truncated=True 与 full_length，原文始终保留在 Artifact 里。
        """

    @abstractmethod
    async def save_bytes(
        self, session_id: str, data: bytes, *, mime_type: str
    ) -> BlobArtifact:
        """存**原始字节**（内容寻址，id 形态见 `BYTE_ARTIFACT_ID_PATTERN`）。

        与文本 `save` 的区别：不做 UTF-8 假设、id 是完整 sha256（非前 16 位）。
        同一字节重复存入必须收敛到同一 id 且只落一份（去重）。
        """

    @abstractmethod
    async def load_bytes(self, artifact_id: str) -> BlobArtifact:
        """按字节 id 读回原始字节（`content` 填充），并做内容寻址自证。

        **读回**语义：id 存在即可读（#830 之后字节对象按内容寻址、可跨会话寻址；
        "谁能读"由调用方的授权闸门负责，不由本方法负责）。not-found 统一
        `KeyError`——与文本路径同契约。
        """

    async def load_uploaded_bytes(self, artifact_id: str) -> BlobArtifact:
        """按字节 id 读回**本会话上传过**的字节（发送侧归属校验用）。

        与 `load_bytes` 的区别只有一问：**"谁的字节"**。`load_bytes` 答"存在吗"，
        本方法答"属于本会话吗"（PRD D5：发送时校验 id "属于本 session 上下文"）。
        不是本会话上传的（含别的会话、从未上传）统一 `KeyError` → 422。

        **没有默认实现，这是刻意的（#933 M-01）**。改动前这里是 `return await
        self.load_bytes(...)`，出处是"字节 key 带会话前缀时命名空间即会话，两者等价"——
        #830 D1 之后这个前提在两个方向上都塌了：

        - 对**全局寻址的 Provider**（Local，以及 #933 起的 S3/MinIO），`load_bytes` 按内容
          寻址、**必然**跨会话读得到 ⇒ 默认实现会把"别的会话/fork 子会话的字节"判成
          "本会话上传过"，发送侧归属闸门（PRD D5）静默失效，且**失败方向是放宽授权**；
        - 对**没有归属事实的替身**（`FakeArtifactStore` 的扁平 dict），默认实现让替身
          "看起来实现了归属"，于是用它写的归属断言全是假绿。

        所以本方法要求每个 Provider**各自回答**"本会话凭什么说这些字节是自己的"
        （`LocalArtifactStore` = 会话回执 hardlink；远端 Provider = 会话回执对象 key）。
        来源: DSH `packages/attachment/attachment/src/index.ts:53-265`——那里的
        `AttachmentStore` 没有任何"默认实现换个语义"的方法，能力缺失一律显式拒绝
        （`saveFile` → `ATTACHMENT_FILES_UNSUPPORTED`），不存在静默回落。

        未实现即 `NotImplementedError`（不是 `KeyError`）：它是**编程错误**，不是
        契约内的"不存在"，不许被 404/422 的错误处理顺手吞掉。
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement load_uploaded_bytes to answer "
            '"are these bytes this session\'s?" (#933 M-01)'
        )


def compute_artifact_id(content: str) -> str:
    """content-hash 寻址：SHA-256 前 16 字符作为 artifact_id。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def compute_byte_artifact_id(data: bytes) -> str:
    """**字节路径**的内容寻址 id：完整 SHA-256，带 `sha256:` 前缀。

    来源: DeepSeek Harness `5badb150` `attachment-local/src/file-store.ts`
    （`AttachmentId("sha256:<hex>")`，MIT）。
    """
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def slice_lines(
    all_lines: list[str],
    *,
    start_line: int | None,
    end_line: int | None,
    keyword: str | None,
    max_lines: int,
    max_chars_per_line: int = 2000,
) -> tuple[list[dict[str, int | str | bool]], bool]:
    """通用行切片逻辑：**artifact 的四个 Provider（Fake / S3 / MinIO / Local）+ Web 工作区文件读取**共用这一份。

    （原为模块私有 `_slice_lines`；#191 的 `GET .../workspace/file` 需要同一套
    "从第 N 行起 / 最多 N 行 / 单行超长怎么标"的语义 ⇒ 提升为公开函数——
    与其在 web 层再写一份切片，不如让两处消费同一个实现。）

    返回 (行列表, truncated)。truncated 是行数截断与字符截断的并集——
    任一发生即 True（spec 06 §4：大 Artifact 不完整灌回 Context）。

    每个超长行按 max_chars_per_line 截断，原文始终完整保留在 Artifact / 文件里。
    截断行额外携带 truncated=True 与 full_length 字段，调用方可凭 line_number
    重新定位（如经 max_chars_per_line 参数放宽上限继续读）。
    """
    indexed = [{"line_number": i + 1, "text": line} for i, line in enumerate(all_lines)]

    if keyword:
        filtered = [entry for entry in indexed if keyword in entry["text"]]
    else:
        s = (start_line or 1) - 1  # 转 0-based
        e = end_line if end_line is not None else len(indexed)
        filtered = indexed[s:e]

    char_truncated = False
    capped: list[dict[str, int | str | bool]] = []
    for entry in filtered:
        text = entry["text"]
        if len(text) > max_chars_per_line:
            char_truncated = True
            capped.append({
                "line_number": entry["line_number"],
                "text": text[:max_chars_per_line],
                "truncated": True,
                "full_length": len(text),
            })
        else:
            capped.append(entry)

    truncated = char_truncated or len(filtered) > max_lines
    return capped[:max_lines], truncated


def slice_artifact(
    artifact_id: str,
    content: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    keyword: str | None = None,
    max_lines: int = 200,
    max_chars_per_line: int = 2000,
) -> ArtifactSlice:
    """`inspect()` 的整段实现：切片 + 组装 ArtifactSlice。

    四个 Provider 的 `inspect()` 差别只在"怎么拿到 content"（内存 dict / S3 / MinIO /
    本地文件），拿到之后这一段逐字相同。此前四份各自复制，`query` 回显字段与
    `total_lines` 的语义就有四处各自演化的空间——现在只有这里一份，
    Provider 只负责 `load()` 后把 content 交进来。
    """
    all_lines = content.splitlines()
    lines, truncated = slice_lines(
        all_lines,
        start_line=start_line,
        end_line=end_line,
        keyword=keyword,
        max_lines=max_lines,
        max_chars_per_line=max_chars_per_line,
    )
    return ArtifactSlice(
        artifact_id=artifact_id,
        lines=lines,
        total_lines=len(all_lines),
        returned_lines=len(lines),
        truncated=truncated,
        query={
            "start_line": start_line,
            "end_line": end_line,
            "keyword": keyword,
            "max_lines": max_lines,
        },
    )


class FakeArtifactStore(ArtifactStore):
    """内存 dict 实现——给单元测试用。不碰网络。

    **本替身没有归属语义，且现在会**大声**失败（#933 M-01）**：`_blobs` 是**按 id 的
    扁平全局 dict**（`save_bytes` 忽略命名空间、跨会话去重），`load_bytes` 返回任意会话
    存进来的 blob ⇒ 它答不了"这些字节属于哪个会话"。改动前它靠继承 ABC 的
    `load_uploaded_bytes = load_bytes` 默认实现，把"存在于全局"当成"属于本会话"，
    发送侧归属闸门在它身上形同虚设且**看不出来**；现在 ABC 不再给那个默认实现，
    本替身调用它会 `NotImplementedError`（响亮的编程错误，而不是假绿）。

    ⇒ **禁止用本替身断言发送侧归属 / 跨会话拒绝**（如"别的会话 send → 422"）：那条闸门
    只对实现了 `load_uploaded_bytes` 的会话感知 Provider（`LocalArtifactStore`、S3/MinIO）
    成立。本替身只用于形状、往返、`inspect` 切片等与会话归属无关的用例；真要归属语义，
    就用 `LocalArtifactStore`（临时目录）或远端 Provider 的 SDK 替身。
    """

    def __init__(self) -> None:
        self._artifacts: dict[str, tuple[Artifact, str]] = {}  # id → (meta, content)
        self._blobs: dict[str, tuple[BlobArtifact, bytes]] = {}  # 字节路径 id → (meta, data)

    async def save(
        self,
        session_id: str,
        content: str,
        *,
        mime_type: str,
        source_tool: str,
        tool_call_id: str,
    ) -> Artifact:
        from agent_harness.storage.sqlite import _utc_now_iso

        artifact_id = compute_artifact_id(content)
        artifact = Artifact(
            artifact_id=artifact_id,
            session_id=session_id,
            size=len(content.encode("utf-8")),
            mime_type=mime_type,
            source_tool=source_tool,
            tool_call_id=tool_call_id,
            created_at=_utc_now_iso(),
        )
        self._artifacts[artifact_id] = (artifact, content)
        return artifact

    async def load(self, artifact_id: str) -> Artifact:
        if artifact_id not in self._artifacts:
            raise KeyError(f"Artifact '{artifact_id}' does not exist")
        meta, content = self._artifacts[artifact_id]
        return meta.model_copy(update={"content": content})

    async def inspect(
        self,
        artifact_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        keyword: str | None = None,
        max_lines: int = 200,
        max_chars_per_line: int = 2000,
    ) -> ArtifactSlice:
        if artifact_id not in self._artifacts:
            raise KeyError(f"Artifact '{artifact_id}' does not exist")
        _meta, content = self._artifacts[artifact_id]
        return slice_artifact(
            artifact_id,
            content,
            start_line=start_line,
            end_line=end_line,
            keyword=keyword,
            max_lines=max_lines,
            max_chars_per_line=max_chars_per_line,
        )

    async def save_bytes(
        self, session_id: str, data: bytes, *, mime_type: str
    ) -> BlobArtifact:
        artifact_id = compute_byte_artifact_id(data)
        blob = BlobArtifact(
            artifact_id=artifact_id,
            session_id=session_id,
            size=len(data),
            mime_type=mime_type,
        )
        # 同 id 覆盖 = 内容寻址去重（同字节序列只留一份）。
        self._blobs[artifact_id] = (blob, data)
        return blob

    async def load_bytes(self, artifact_id: str) -> BlobArtifact:
        if artifact_id not in self._blobs:
            raise KeyError(f"Blob artifact '{artifact_id}' does not exist")
        blob, data = self._blobs[artifact_id]
        return blob.model_copy(update={"content": data})
