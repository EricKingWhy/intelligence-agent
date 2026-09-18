"""Provider failure 语义的 model 层 owner（T03/#239）。

审计 finding：Agent Core 曾直接持有 vendor/protocol failure 字符串（`agent/runtime.py`
里的 `_PROVIDER_FAILURE_MARKERS` 与 DSML wire marker），于是"新增/调整一个供应商的错误
语义"要改 Agent Loop。本模块把这两样收进 model 层，Runtime 只消费这里给出的
reason / 固定文案 / 布尔判定。

两个面，互不重叠：

- :func:`classify_provider_failure`：``str(error)`` 命中供应商错误标记 → 项目自有
  reason（配 :data:`PROVIDER_FAILURE_MESSAGES` 的固定可读文案）。**只做分类**——
  provider 回显原文绝不外传（脱敏不变量 OBS-008）。机制全文见
  ``docs/adr/0033-run-failure-attribution-surface.md`` §2.1。
- :func:`has_malformed_tool_call_markup`：正文含协议保留标记 = 网关没把工具调用解析成
  结构化字段（DSML 泄漏），按模型故障处理，绝不把这段标记文本当最终回答持久化。

与 ``model/fallback.py`` 的分工（不变量 #9 的邻居）：fallback 判「是否值得换一台模型」
（retryability，结构化判据），本模块判「失败是什么」（attribution，按供应商错误文本）；
共用异常对象，但判据与消费者不同，故不合并——本模块的消费者是失败终态的载荷。
"""

from __future__ import annotations

#: DeepSeek 系（主/备 provider 均为 deepseek 模型）工具调用的协议保留标记。
#: 碎片化流下网关的 DSML 工具调用可能未被解析成结构化 tool_calls 而以乱码
#: content 泄漏（冒烟实测 session 7afd328a：流式标记混进最终回答）——含此
#: 标记的 content 绝不可能是合法模型回答，按模型故障处理（走统一失败兜底，
#: 决不伪造 run/completed）。用全角 ｜ 保留标记做判据以杜绝误伤正常讨论文本。
_DSML_MARKUP_MARKER = "<｜DSML｜"

# provider 侧失败的分类：识别后失败事件升级为固定可读文案，消费者（UI 事件检查器）
# 无需翻服务端日志。只做分类——provider 回显原文绝不进事件（脱敏不变量，见
# append_model_failed）。
#
# 两类历史来源：
# - 内容审查拒绝：阿里云 data_inspection_failed（2026-09-11 真实案例：百炼端点对含
#   检索网页文本的二次调用 400）。
# - 账户 / 鉴权 / 模型不存在（#218，2026-09-17 真机案例：计费账户被冻结时界面只说
#   ``BadRequestError``——该类型名横跨「欠费 / 鉴权 / 模型名错」三种完全不同的
#   处置路径，不构成有效信息）。实测日志原文见
#   docs/LIVE_BROWSER_TEST_20260917.md §2.1。
CONTENT_MODERATION_REASON = "provider_content_moderation"
CONTENT_MODERATION_MESSAGE = "provider 内容审查拒绝输入（可能因检索到的网页文本）"
PROVIDER_ACCOUNT_UNAVAILABLE_REASON = "provider_account_unavailable"
PROVIDER_ACCOUNT_UNAVAILABLE_MESSAGE = (
    "模型供应商账户不可用（欠费 / 配额耗尽 / 账户被冻结），"
    "请到供应商控制台检查计费与配额"
)
PROVIDER_AUTH_REASON = "provider_auth_failed"
PROVIDER_AUTH_MESSAGE = (
    "模型供应商鉴权失败（API Key 无效或无权限），请检查供应商凭证配置"
)
PROVIDER_MODEL_NOT_FOUND_REASON = "provider_model_not_found"
PROVIDER_MODEL_NOT_FOUND_MESSAGE = (
    "模型不存在，或当前账户无权访问该模型，请检查模型名与开通状态"
)

#: 未分类失败的固定可读兜底（#222）。**只代入异常类型名**——类名不是 provider
#: 回显正文，不越 OBS-008 的脱敏边界（与 ``model/failed`` 的
#: "model call failed: {error_type}" 同源）。不写"请重试"：未分类里重试有效无效
#: 都有，承诺不了。口径见 docs/adr/0033-run-failure-attribution-surface.md §2.4。
UNCLASSIFIED_FAILURE_MESSAGE = (
    "运行失败（{error_type}），未分类异常；完整原始信息见后端日志"
)

#: 分类表：``str(error)`` 里的小写标记子串 → 分类 reason。顺序即优先级。
#:
#: ⚠ 匹配面是整个 ``str(error)``（含 provider 错误体里的 ``code`` **与** ``message``
#: 自然语言），不是在解析错误码——所以标记是"**从错误码里挑的词**"，不是"只可能出现在
#: 错误码里"。实测例子：``insufficient_quota`` 那条载荷同时含 ``billing details``，
#: 而 ``billing`` 在表里更靠前，于是走 ``billing`` 命中同一分类（结果一致，故不修顺序；
#: 但别以为顺序不影响）。
#: 命中不了本表的错误码保持"只带类型名"的原行为——典型是限流的
#: ``rate_limit_exceeded``：临时态、属模型 fallback 责任域，不做可读文案；
#: 而 ``billing`` 是**账户级硬阻塞**，两者处置不同（实测那条：计费账户被冻结）。
_PROVIDER_FAILURE_MARKERS: tuple[tuple[str, str], ...] = (
    ("data_inspection_failed", CONTENT_MODERATION_REASON),
    ("billing", PROVIDER_ACCOUNT_UNAVAILABLE_REASON),
    ("insufficient_quota", PROVIDER_ACCOUNT_UNAVAILABLE_REASON),
    ("quota_exceeded", PROVIDER_ACCOUNT_UNAVAILABLE_REASON),
    ("account_deactivated", PROVIDER_ACCOUNT_UNAVAILABLE_REASON),
    ("account_suspended", PROVIDER_ACCOUNT_UNAVAILABLE_REASON),
    ("invalid_api_key", PROVIDER_AUTH_REASON),
    ("incorrect_api_key", PROVIDER_AUTH_REASON),
    ("invalid_organization", PROVIDER_AUTH_REASON),
    ("model_not_found", PROVIDER_MODEL_NOT_FOUND_REASON),
)

#: 分类 reason → 固定可读文案（``append_model_failed`` 的 ``readable_message``）。
#: 与上表**必须键集一致**（有无文案的对账用例）：分类命中而文案缺键 ⇒ 取文案时
#: KeyError，会被失败路径的兜底 except 吞掉，连 run/failed 一起丢——把这件事堵在
#: 提交前，而不是运行期静默。
PROVIDER_FAILURE_MESSAGES: dict[str, str] = {
    CONTENT_MODERATION_REASON: CONTENT_MODERATION_MESSAGE,
    PROVIDER_ACCOUNT_UNAVAILABLE_REASON: PROVIDER_ACCOUNT_UNAVAILABLE_MESSAGE,
    PROVIDER_AUTH_REASON: PROVIDER_AUTH_MESSAGE,
    PROVIDER_MODEL_NOT_FOUND_REASON: PROVIDER_MODEL_NOT_FOUND_MESSAGE,
}


def classify_provider_failure(error: BaseException) -> str | None:
    """错误文本命中分类表 → 返回分类 reason，否则 None（保持原行为）。

    按文本匹配而不是 SDK 异常属性：openai SDK 对 SSE 形状的错误响应
    （``data: {...}``）解析不出结构化 body，错误码只存在于 str(error) 里；且按文本
    匹配不绑具体 SDK 版本与厂商（各家 OpenAI 兼容端点形状不一）。小写化后再比：
    ``str(error)`` 的大小写由供应商决定，不是契约。
    """
    text = str(error).lower()
    for marker, reason in _PROVIDER_FAILURE_MARKERS:
        if marker in text:
            return reason
    return None


def has_malformed_tool_call_markup(content: str) -> bool:
    """正文含 DSML 协议保留标记 ⇒ True（网关没把它解析成结构化 tool_calls）。

    只看标记本身；"无结构化 tool_calls"那一半判据在调用点（Runtime 同时知道响应
    形状），本函数不接收 AIMessage，免得 model 层反向依赖 langchain 的响应对象。
    """
    return _DSML_MARKUP_MARKER in content
