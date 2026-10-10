"""附件字节的存储布局常量：**三个 Provider 共用一份**（#933 M-01）。

为什么单独成模块：字节对象改全局内容寻址根之后，"全局根叫什么、候选怎么拼"不再是
Local Provider 的私有知识，而是**跨 Provider 的契约**——三处各写一份字面量，改一处
就会静默错配（写进 A、从 B 读，症状与 `artifact_select` 要消灭的那个同族）。

`#830 D1` 定的布局（本模块是它的常量落点，语义叙述在
`docs/agents/830-d1-global-attachment-store-design-proposal.md` §3）：

    <root>/.attachments/objects/<sha[:2]>/<sha>        全局字节对象（跨会话去重）
    <root>/<session_id>/attachments/objects/<sha[:2]>/<sha>  Local 的会话回执（hardlink）
    <session_id>/attachments/<sha>                     远端 Provider 的会话回执对象

**为什么全局根用点号目录**：`SESSION_KEY_PATTERN = [A-Za-z0-9_-]{1,128}` **不**匹配前导点，
因此名为 `attachments` 的会话**不可能**与全局根同名——"用 setting + session_id 拼路径"
的删除入口（`discard_local_artifacts` 等）在构造上碰不到全局对象根
（同 `sandbox/registry.py` 的 `.fork-tmp` 先例）。远端 key 同理由此不可撞。
"""

from __future__ import annotations

#: 全局内容寻址根在 artifact 根 / bucket 下的目录名。
GLOBAL_ATTACHMENT_DIRNAME = ".attachments"

#: 全局根下放字节对象的子目录（与文本 artifact 的 key 空间分开）。
BYTE_OBJECTS_DIRNAME = "objects"

#: 会话段下放"本会话上传回执"的子目录（远端 Provider 的 key 前缀；与全局化之前的
#: 旧字节 key 同形，升级兼容因此不需要额外分支）。
SESSION_ATTACHMENTS_DIRNAME = "attachments"

#: 远端 Provider 的全局对象 key 前缀（`<prefix>/<sha[:2]>/<sha>`）。
GLOBAL_BYTE_KEY_PREFIX = f"{GLOBAL_ATTACHMENT_DIRNAME}/{BYTE_OBJECTS_DIRNAME}"

__all__ = [
    "BYTE_OBJECTS_DIRNAME",
    "GLOBAL_ATTACHMENT_DIRNAME",
    "GLOBAL_BYTE_KEY_PREFIX",
    "SESSION_ATTACHMENTS_DIRNAME",
]
