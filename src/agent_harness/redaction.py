"""凭证值脱敏（`#317` T9 / ADR-0048 D3）——跨层共享的**唯一**算法。

放在顶层而不是 `transport/`：消费方横跨两层——`transport.contract.redact_command_summary`
（出站的命令摘要）与 `agent.guards`（stuck 指纹的规范化）。让 Core 反过来导入传输层
是把依赖方向倒过来（Core 是内核，传输层是外沿），所以这条纯文本规则住在两者之间。

**只做凭证替换，不做路径 scoping**：路径是参数语义的一部分——把 `/a/b.txt` 与
`/a/c.txt` 都折叠掉会让两个不同的动作撞成同一个指纹，检测器就把"换了目标"误读成
"原地打转"。路径 scoping 是 `redact_command_summary` 自己的事，不在本模块。
"""

from __future__ import annotations

import re

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(--?(?:token|password|passwd|secret|api[-_]?key|authorization)|"
    r"(?:token|password|passwd|secret|api[-_]?key|authorization))\s*(?:=|:)\s*([^\s]+)"
)
_GIT_HEADER_SECRET = re.compile(
    r"(?i)(-c\s+http\.[^=\s]*extraheader\s*=\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s]+(?:\s+[^\s]+)*)"
)
_GIT_CONFIG_SECRET = re.compile(
    r"(?i)(-c\s+(?:credential\.[^=\s]+|http\.[^=\s]+|url\.[^=\s]+))="
    r"(?:\"[^\"]*\"|'[^']*'|[^\s]+)"
)
_GIT_URL_USERINFO = re.compile(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@")


def redact_secret_values(text: str) -> str:
    """凭证值 → `<redacted>`，**不动路径**（`#317` T9 / ADR-0048 D3）。

    stuck 指纹要比较"两次调用是不是同一个动作 / 同一个观察"，路径与参数值属于语义；
    凭证值相反——它 MUST NOT 进指纹、MUST NOT 进持久化（`02 §5.3`）。

    非字符串 / 空输入原样返回：本函数是过滤层，不是校验层。

    **`Bearer` 规则必须排在泛化的赋值规则之前**（T9 实测）：`Authorization: Bearer <token>`
    里，泛化规则的 `([^\\s]+)` 会吃掉字面量 `Bearer`，把 token 本体留在原地——
    替换后的文本再没有 `Bearer ` 可匹配，于是 token 原样进指纹 / 进事件。先跑 Bearer
    规则把它整段折掉，泛化规则就没有可漏的尾巴了。
    """
    if not isinstance(text, str) or not text:
        return text
    redacted = re.sub(r"(?i)(Bearer\s+)[^\s]+", r"\1<redacted>", text)
    redacted = _SECRET_ASSIGNMENT.sub(r"\1=<redacted>", redacted)
    redacted = _GIT_HEADER_SECRET.sub(r"\1<redacted>", redacted)
    redacted = _GIT_CONFIG_SECRET.sub(r"\1=<redacted>", redacted)
    return _GIT_URL_USERINFO.sub(r"\1<redacted>@", redacted)
