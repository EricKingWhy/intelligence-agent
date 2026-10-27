"""全局运行配置：只定义"运行需要什么"，从 .env 读取。"""

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

from agent_harness.attachments.validation import parse_allowed_media_types

#: .env 锚定到仓库根（config.py 位于 <root>/src/agent_harness/）——
#: 相对路径 ".env" 依赖 CWD，从其它目录启动 uvicorn/CLI 会静默加载不到
#: 配置，错误推迟到首个请求内才爆（R6-5）。安装为 site-packages 时锚点
#: 无意义但无害（该路径下不存在 .env，退化为环境变量配置）。
_REPO_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger(__name__)

#: 已退役键（#1535）：`MILVUS_COLLECTION` 无任何运行时消费者，检索目标只由
#: `knowledge_collection`（`KNOWLEDGE_COLLECTION`）决定。部署 `.env` 里残留该键时
#: **只告警、不拒绝**——`extra="ignore"` 的启动鲁棒性（见下方 `model_config`）不变，
#: 单为它破例改成拒绝会让"他应用变量混入"这类既有容忍面变成启动崩溃。
_RETIRED_KEYS: tuple[str, ...] = ("MILVUS_COLLECTION",)

#: 上下文窗口五挡位（#1197 项 1）。**唯一的**挡位清单：`Settings.max_context_tokens`
#: 的构造期白名单、API 的 PATCH 校验、前端选择器三处都读它——挡位表分列两处就会出现
#: 「面板能选、后端 422」的错位。
#:
#: 为什么是白名单而不是 `ge=1` 区间：票面要求挡位外的值**启动期响亮失败**，区间校验
#: 会放行 100001 这种既非挡位、又让 70%/85% 线落到非整数上的值。为什么不做
#: 挡位×模型联动：那是二期（票面「第一版只做挡位校验，不做模型联动」）。
CONTEXT_WINDOW_TIERS: tuple[int, ...] = (100_000, 200_000, 256_000, 300_000, 500_000)

#: `ask_user` 超时档（秒）的**唯一**合法域（#1204 / #1224 / #1284）。两处消费：
#: `Settings` 构造期 validator 与 `AskUserTool.__init__` 的域校验。分列两处会让
#: "配置能起、工具构造失败"或"部署能改、载荷不认"成为可能。
ASK_USER_QUESTION_TIMEOUT_TIERS: frozenset[int] = frozenset({60, 300, 600})

#: `ask_user` 超时的缺省档（秒）。`Settings` 与 `AskUserTool.__init__` 共用这一处，
#: 不再各写一份 `300`；两者相等仍由 `tests/tools/test_ask_user.py` 机械钉住。
ASK_USER_QUESTION_TIMEOUT_DEFAULT: int = 300


class Settings(BaseSettings):
    # extra="ignore"：部署机 .env 常混有编辑器/部署工具/其它应用的变量；
    # 默认 extra='forbid' 会把它们当成致命错误让启动直接崩。外键忽略。
    model_config = {"env_file": str(_REPO_ROOT / ".env"), "extra": "ignore"}

    model_provider: str = "deepseek"
    model_name: str = ""
    # SecretStr：与 milvus_token / embedding_api_key 一致——model_dump() / repr()
    # 脱敏为 **********，避免任何把 Settings 转储进日志/调试器/异常页的路径泄漏 live key。
    model_api_key: SecretStr = SecretStr("")
    model_base_url: str = ""
    # Optional AGENT_MODELS / provider selection for context summaries; None keeps
    # the existing primary-model behavior.
    summary_model: str | None = None

    temperature: float = 0.2
    # 流式守卫（秒，逐项 ≤0 关闭）：idle = N 秒无新 chunk（死连接）；
    # total = 整条流必须 N 秒内完成（慢滴漏，冒烟实测 10 分钟场景）。
    # 超时抛 ModelStallError（瞬时）→ fallback 接管 / 统一失败兜底。
    model_stream_idle_timeout: float = 60.0
    model_stream_total_timeout: float = 600.0
    # 进程级模型调用并发闸（#89）：parent+child 共享上限，防 TPM/QPM 限流与
    # 机器过载。≤0 = 关闭。TPM 令牌桶限流器 DEFER（ADR-0015）。
    model_max_concurrency: int = 3
    # Deployment hard ceiling：单个 AgentRuntime 实例的 local turn fuse（#308 / ADR-0044
    # D1，权威表在 `02 §5.1`）。operator 的政策旋钮——**下调**它是本特性的意图，上调也是
    # 部署者的显式选择（本层就是最高层，"越权"只在它之下成立）。请求覆盖只能收窄到这个
    # 值之下，越过即 422（判定见 `agent/budget.py`）。
    #
    # 字面量刻意与 `agent.budget.DEFAULT_MAX_AGENT_TURNS` 分列两处而**不 import**：
    # config 是依赖图最底层，反向 import `agent_harness.agent`（包 `__init__` 会拉
    # runtime → model.config → 回到本模块）会踩部分初始化循环。两者相等由
    # `tests/agent/test_local_budget_resolution.py::test_settings_default_matches_contract`
    # 机械钉住——漏了那条就是"规格说 500、部署默认另一个数"。
    local_max_agent_turns: int = Field(default=500, ge=1)
    # #1359 阶段 3 上线开关（票面发布说明：「首次上线时自动续跑默认关闭」）。
    # True = run 终态后按 `_maybe_queue_goal_round` 的准入自动续跑；False = 目标
    # 仍是可管理的持久对象（`/goal` 全生命周期可用），但绝不自己拉下一轮。
    # 消费点唯一：`SessionService._maybe_queue_goal_round` 入口短路——放在那里
    # 而不是 `set_goal`，因为 activation 是进程内状态，重启后「默认关闭」仍成立。
    goal_auto_round_enabled: bool = False
    # 交互式审批等待上限（秒）：无人决策 = fail-closed 默认拒绝（PRD T6 §2.2 C）。
    # ≤0 = 无限等待（关闭 fail-closed，保留旧行为）。默认 300s：足够人类走开再回来，
    # 又不让一个没人管的危险工具无限期挂住 run。
    approval_timeout_seconds: float = 300.0
    # #1204：通用提问 `ask_user` 的超时档（秒，抄 Claude Code `askUserQuestionTimeout`
    # 的 60s / 5m / 10m）。计时由**服务端**从提问落盘起持续累计，不看前端连接数
    # （#1320：真实 Web 前端在 run/paused 上只走 HTTP 轮询，连接数不是在场证据）；
    # 到点后 run 自动继续并注入"用户可能不在"系统消息，**不替用户选择任何选项**。
    # 取值只允许 {60, 300, 600}（下面的 validator 在构造期响亮拒绝其它值）。
    ask_user_question_timeout: int = ASK_USER_QUESTION_TIMEOUT_DEFAULT
    # Bash 工具的执行预算（秒）。ADR-0039（含 2026-09-22 附录）：#244 冻结
    # 「ToolExecutor 是唯一 deadline owner、Local/Docker 默认有效预算同为 60 秒」，
    # 本字段只提供 Executor 消费的预算输入，不与它争 owner。
    # 非法值（≤0 / nan / ±inf）在 **Settings 构造期**响亮失败——构造期即启动期，
    # 所以"没有预算"或"无穷预算"不会被静默带进运行时（#244 AC5）。
    # 消费点只有装配层（assembly 的 BUILTIN_LOCAL_TOOLS 循环）；接不上就成死键，
    # 由 tests/test_assembly_bash_budget.py 钉住。
    bash_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    # Model Fallback 两级链（ADR-0014 决策 14）：FALLBACK_MODEL_PROVIDER 为空 =
    # 单级（无 fallback）。fallback key 同 SecretStr 脱敏待遇（活密钥）。
    fallback_model_provider: str = ""
    fallback_model_name: str = ""
    fallback_model_api_key: SecretStr = SecretStr("")
    fallback_model_base_url: str = ""
    # jwt_secret 泄漏等于可伪造任意身份，与 API key 同一脱敏待遇。
    jwt_secret: SecretStr | None = None
    milvus_uri: str = ""
    milvus_token: SecretStr = SecretStr("")
    # Knowledge 域独立 Collection（ADR-0013 决策 13）：必填无默认——
    # 不配 = knowledge capability 缺席降级，绝不静默写默认库。
    knowledge_collection: str = ""
    # sufficient 阈值（ADR-0013 决策 9，用户拍板 0.6：低于此证据太弱，
    # 宁可如实标记不足防幻觉）。hits 照返，标记是诚实信号不是结果开关。
    knowledge_min_score: float = Field(default=0.6, ge=0, le=1)
    # Web Search（ADR-0014 决策 9/10）：TAVILY_API_KEY 为空 = websearch
    # capability OPTIONAL_RUNTIME 降级缺席（不配 = 不联网，绝不静默触网）。
    tavily_api_key: SecretStr = SecretStr("")
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_dimensions: int = Field(default=1024, gt=0)
    # BUG-014：记忆检索外层超时（此前 context provider 写死 5s，比 embedding SDK
    # 的 15s 还紧——代理转发场景必超时）。Settings 注入式，与既有超时字段同风格。
    memory_search_timeout_seconds: float = Field(default=10.0, gt=0)
    # R11（#298）：「Jobs are serialized per user. Global formation concurrency defaults
    # to four and is configurable.」——**按用户串行**在数据库里（`jobs.claim` 的自占用
    # 子查询，跨重启有效），这一项是**本进程在飞记忆作业数**的上限。
    # `gt=0` 不是形式约束：0 会让服务循环派不出任何 job，表现为"记忆永远不形成"——
    # 配错必须响亮失败。消费点唯一：`memory/v2/assembly.build_memory_formation`。
    memory_v2_max_concurrency: int = Field(default=4, gt=0)
    # #1039：记忆作业的两个模型角色（PRD §5.3 第 3 条修订）——**运营者可配、默认不变**。
    # 取值 = 内置 preset 名（如 `senseaudio`）或 #203 自定义供应商的 `provider:model`
    # 命名空间（复用 `from_custom_provider` 的寻址，不另造一套）。映射是**应用级**
    # （部署者改、重启生效）：装配层进程级解析一次（`memory/v2/roles.py`），与面板级
    # per-user 降级开关（`memory_v2_settings.fallback_enabled`）分层——这正是 #1039 的
    # 两条裁决各归其层的落点。默认字面量与 `roles.MEMORY_PRIMARY_PROVIDER` /
    # `roles.MEMORY_FALLBACK_PROVIDER` 分列两处而刻意不 import（config 是依赖图最底层，
    # roles 反向依赖它，import 会踩部分初始化循环）；两者相等由
    # `tests/memory/v2/test_v2_roles.py::test_the_role_mapping_defaults_to_the_providers_the_prd_names`
    # 钉住。
    memory_primary_provider: str = "senseaudio"
    memory_fallback_provider: str = "qwen"
    # #1353 M7：后台记忆形成的人面向通知档位（上游 `notification_mode`）。
    # `off` 不发 `memory/human_updated`；`on` 只发计数/指纹；`verbose` 另把内容预览
    # 写进日志（事件载荷不变——不变量 #22）。部署级（重启生效），与面板级
    # `write_approval` / `fallback_enabled` 分层。消费点两处，都在
    # `memory/v2/assembly.py`：`build_memory_formation`（形成通知）与
    # `build_memory_v2_service` → `MarkdownMemoryStore`（漂移通知，#1440 P3-3）。
    memory_notification_mode: Literal["off", "on", "verbose"] = "on"
    # #1441 §1（F-09）：curated memory 的容量预算（字符数，与模型无关）。上游
    # `hermes_cli/config_defaults.py:1321-1322` 同值同单位，经
    # `tools/memory_tool.py` 的 `load_on_disk_store` 读 `memory` 配置段。消费点两处，
    # 同一份预算：`memory/v2/assembly.build_memory_v2_service`（运行时）与
    # `memory/v2/migration.migrate_active_records`（CLI `memory migrate`，由 `cli.py` 传入）。
    # 部署级（重启生效）；`MarkdownMemoryStore` 的构造参数仍可覆盖，测试路径不变。
    memory_char_limit: int = Field(default=2200, gt=0)
    user_char_limit: int = Field(default=1375, gt=0)

    max_context_tokens: int = 200_000
    auto_compact_threshold: float = 0.70
    hard_guard_threshold: float = 0.85
    # #1420：上下文洞察独立模块开关（票面「不碰」段定义）。关闭时三组洞察路由
    # **不注册**（直连 404），Core 路由零影响（不变量 #21）；capability manifest
    # 同步下发同名布尔，前端据此显隐一级入口（标志缺失 = 关闭）。
    insights_enabled: bool = True
    # #1026：摘要请求墙钟随请求规模线性缩放（base + kt × slope，消融候选①）。
    # base 默认 30s 维持小请求的既有墙钟；slope 默认 0.25s/kt 使 ~167k 请求
    # 得 ~71.75s（阶段 A 实测锚：同路径 16.8s 成功、30s 恒败 ⇒ ≥60s 需求）。
    # slope=0 即固定墙钟（消融候选②的配置形态）。消费点：builder → compactor。
    summary_timeout_seconds: float = Field(default=30.0, gt=0)
    summary_timeout_per_kilotoken_seconds: float = Field(default=0.25, ge=0)
    # #1279：重试维度的墙钟放大——第 n 次尝试 = min(规模公式 × multiplier^(n-1),
    # max_seconds)。默认 2× / 300s：慢模型（hy4-preview-f 单轮 45–60s）第二次
    # 有机会完成；上限保证不会无限等待（再超时照常落 compaction_failed）。
    # multiplier 必须 ≥1 且有限（(0,1) 会把重试墙钟缩到 attempt 1 以下，与
    # "放大"相悖；allow_inf_nan=False：inf 等价于取消失败语义）；
    # max_seconds 必须正且有限。
    summary_timeout_attempt_multiplier: float = Field(
        default=2.0, ge=1, allow_inf_nan=False)
    summary_timeout_max_seconds: float = Field(
        default=300.0, gt=0, allow_inf_nan=False)
    # #1026：分块摘要（票面修法②）。摘要请求估算超过 hard_guard × ratio 即走
    # 分块（票面"如 > hard_limit 一半"）；summary_chunk_max_tokens 是单块输入
    # 的 token 上限（块内超大原子消息确定性 head/tail 裁剪到界）。
    summary_chunk_request_ratio: float = Field(default=0.5, gt=0, le=1)
    summary_chunk_max_tokens: int = Field(default=24_000, gt=0)
    # W-29 (#383)：进度清单兜底重注入周期（每 N 条投影消息，PRD §4.6 的
    # Cline Focus Chain 默认值 6，可配置）。
    plan_reinject_every_messages: int = Field(default=6, gt=0)
    artifact_overflow_chars: int = 2000
    # W-31.3（#415）：子代理结论 ref 化阈值（token）——delegate 回传的 summary 估算
    # token 超过本值时，全文外置 ArtifactStore，父窗只收截断摘要行 + summary_ref
    # （不变量 #15）。0 = 关闭本特性；负值由 ge=0 响亮拒绝。
    subagent_summary_overflow_tokens: int = Field(default=2000, ge=0)
    # W-31.2（#414）：确定性裁剪的两个参数（Settings → ContextBuilder →
    # ToolResultPruner）。keep_recent_tool_results = 最近 N 条 tool 结果窗口豁免
    # （按全量 tool/result 事件序计，含失败结果），0 = 关闭窗口豁免；
    # clear_at_least_tokens = 一轮裁剪的总收益估算（原文 − 骨架，token）低于该值
    # 则本轮整体不裁，0 = 关闭收益门。默认值对照 Anthropic 官方 context-editing
    # 示例（clear_tool_uses_20250919）；负值由 ge=0 响亮拒绝。
    keep_recent_tool_results: int = Field(default=3, ge=0)
    clear_at_least_tokens: int = Field(default=5000, ge=0)
    # Phase Multiturn T5 (#135)：MinIO 作为大产物外置对象存储。
    # 与 artifact_store_* 字段独立——MinIO 用于 tool result 外置，
    # artifact_store_* 用于 inspect_artifact 的 S3 兼容存储。
    # 空值 = MinIO 未配置，tool result 不外置（fail-open）。
    minio_endpoint: str = ""
    minio_access_key: SecretStr = SecretStr("")
    minio_secret_key: SecretStr = SecretStr("")
    minio_bucket: str = ""
    artifact_store_endpoint: str = ""
    artifact_store_bucket: str = ""
    # S3 密钥泄漏等于丢失整个 artifact bucket 的写权限，同一脱敏待遇。
    artifact_store_access_key: SecretStr = SecretStr("")
    artifact_store_secret_key: SecretStr = SecretStr("")
    artifact_store_region: str = ""

    # Capability / Plugin 显式配置（spec 08 §6 V1）：JSON 字符串，
    # 形状 {"<name>": {"provider": "...", "enabled": bool, "options": {...}}}。
    capabilities: str = ""
    # Persona（ADR-0023 D10）：env JSON，形如 {"prefix":"…","suffix":"…"}；
    # 空 = 无 persona = 零行为变化。与 capabilities 同形制（原始 str，
    # 解析器负责校验并显式失败）。
    agent_persona: str = ""
    # Skills 全局目录（spec 09 §2）；项目目录是 <workspace>/skills/。
    skill_global_dir: str = ""
    # Repository instruction context is read as untrusted, bounded model input.
    project_instructions_file_max_bytes: int = Field(
        default=32 * 1024, ge=1, le=1024 * 1024,
    )
    project_instructions_total_max_bytes: int = Field(
        default=128 * 1024, ge=1, le=4 * 1024 * 1024,
    )

    log_level: str = "INFO"
    workspace_dir: str = ".agent/workspace"
    # 前端构建产物目录（W-21 D3 / #815）：空串 = mount_static 的既有默认
    # `<repo>/web/dist`。Electron 外壳在打包形态下经 env `WEB_DIST_DIR` 传安装
    # 目录里的 `resources/web`，服务端不猜安装布局；不设时行为完全不变。
    web_dist_dir: str = ""
    # #1483：本机控制面默认只接受精确 loopback Origin / Host。额外网页来源或
    # 特殊 Host 必须逐项显式配置（逗号分隔），禁止用 ``*`` 把边界重新打开。
    control_plane_allowed_origins: str = ""
    control_plane_allowed_hosts: str = ""
    # artifact 本地落盘根目录（spec 06 §3 的默认 Provider：Local filesystem，
    # "开发/小型部署"）。落盘形态是 `<artifact_dir>/<session_id>/<artifact_id>`——
    # 与对象存储的 key 约定 `{session_id}/{artifact_id}` 逐段同构，三个 Provider
    # 因此同形，读取接口保持 Provider 无关。
    #
    # 为什么**独立于** workspace_dir（而不是派生）：ADR-0027 之后 workspace 可能指向
    # **用户的真实仓库**，artifact 绝不能落进去——那条路径既要能被会话硬删清理
    # （ADR-0029 D2：只删 harness 自己拼出来的路径），又不能碰用户目录。
    # 默认值落在 `.agent/` 下与 workspace 同族（.gitignore 已整目录忽略运行时产物）。
    artifact_dir: str = ".agent/artifacts"
    # #822 / MM-01：用户图片附件入站上限（D12/D11，默认取 DSH 一组：20 MiB/图、
    # 20 张/消息、200 MiB/消息、64M 像素、8192px/边）。服务端权威强制；MM-01 只
    # 消费单张字节 / 像素 / 边长三个（数量与总字节在发送端点，属 MM-02/03）。
    attachment_max_image_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    attachment_max_images_per_message: int = Field(default=20, ge=1)
    attachment_max_message_image_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    attachment_max_image_pixels: int = Field(default=64_000_000, ge=1)
    attachment_max_image_dimension: int = Field(default=8192, ge=1)
    # 允许的图片 media types（逗号分隔）。解析规则单点在
    # `attachments.validation.parse_allowed_media_types`（#939 M-15 从 `attachments.types`
    # 迁到中性模块，断掉 config ↔ attachments 的环）；本字段在**构造期**（= 启动期）
    # 就校验它（下面的 validator），配错即响亮失败，不会拖到请求路径才 500。
    attachment_allowed_media_types: str = "image/png,image/jpeg,image/webp,image/gif"
    # #823 / MM-02（B2）：发给视觉模型时图片块的 `detail` 档位（PRD D4/D11 的
    # "detail 默认值"可配置）。默认 `auto`（OpenAI 取值之一）；装配层把它接进
    # `ContextBuilder`，投影出口翻译 provider 载荷时使用。
    image_detail: str = "auto"
    # #823 / MM-02（B4）：发送前归一化目标（PRD D11 的"发送前缩放档"可配置）。
    # 默认取 DSH 一组：长边 ≤ 2048px、编码后 ≤ 4 MiB。注意**字节目标是尽力而为**
    # （`_encode_within` 质量阶梯用尽仍可能 > 目标，见 `attachments/normalize.py`），
    # 不是硬上限——名字/注释如实，不谎称硬约束。
    image_normalize_max_dimension: int = Field(default=2048, ge=1)
    # 注意（#935 同步义务）：token 估算侧 `context.tokens.IMAGE_MAX_DIMENSION` 必须 ≥ 本值，
    # 否则"尺寸未知回退 = 上限²"的保守契约会被静默击穿（估算封顶在小值、实际可发更大图）。
    # 调大本值时务必同步核对 `IMAGE_MAX_DIMENSION`（见其 docstring 的同步义务）。
    image_normalize_max_bytes: int = Field(default=4 * 1024 * 1024, ge=1)
    # detached-run 孤儿回收宽限期（秒，ADR-0016 §2.1）：零订阅者连续超过
    # 该时长 → run 被取消收尾（run/failed(reason=orphaned)）。有订阅者期间
    # 不计时；≤0 = 不回收（不推荐：无人观看的 run 会烧到自然终态）。
    run_disconnect_grace_seconds: float = 300.0
    # 会话级可选模型 catalog（ADR-0016 §5，C6）：JSON 数组
    # [{"name", "provider", "model_name", "base_url"?, "api_key"?, "temperature"?}]。
    # api_key 缺省回落 MODEL_API_KEY，base_url 缺省回落 provider 预设。
    # 空 = 无可选模型（GET /api/models 只列默认链，model 参数一律 422）。
    # SecretStr：JSON 里可带条目级 api_key（活密钥），dump()/repr() 一律脱敏。
    agent_models: SecretStr = SecretStr("")

    # #203 / ADR-0032：自定义供应商存储。非密配置落**用户级全局** JSON
    # （作用域 = 全局，用户裁定；**不得**放进 workspace——那是 per-session 的；
    # 相对路径依赖 CWD 的教训与 .env 同源，默认锚用户主目录）；
    # 密钥只进凭据管理器（keyring），该文件不含任何密钥字段。
    provider_store_path: str = str(Path.home() / ".agent-harness" / "model-providers.json")
    # 连接测试超时（秒，§6.2）：固定参数之一，默认 15s——测试不该等 300s。
    model_test_timeout_seconds: float = 15.0
    # #1485 端点误配置守卫（裁决 25 B）：默认拦云元数据 / link-local，loopback 与
    # RFC1918 照旧放行。所有者自用端点确实落在这两类网段时，在此显式登记例外
    # （逗号分隔 `host` 或 `host:port`；IPv6 带端口写 `[fe80::1]:8080`）。
    # 只认 env/.env——web 请求体 `extra="forbid"`，外部输入改不了这一项。
    model_endpoint_allowlist: str = ""

    # Langfuse 旁路观测（ADR-0018 D2/D6，首个 OPTIONAL_OBSERVABILITY 实现）：
    # key 空 = 旁路完全缺席（懒加载，零 import 开销）。key 同 SecretStr 脱敏待遇。
    langfuse_public_key: SecretStr = SecretStr("")
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_base_url: str = ""
    # trace 上云内容边界：full=完整输入输出（自有 dev 项目默认）；
    # redacted=只传 metadata + 截断/摘要。非法值按 full 处理并告警。
    langfuse_trace_content: str = "full"
    # Langfuse 一等字段（D7 DEFER 批）：environment 区分 prod/staging/dev（缺省
    # development，绝不落入 default）；release 标版本/SHA（空=不塞，SDK 自决）。
    langfuse_tracing_environment: str = "development"
    langfuse_release: str = ""

    @model_validator(mode="before")
    @classmethod
    def _warn_on_retired_keys(cls, data: Any) -> Any:
        """残留的已退役键被忽略时**告警**（#1535 AC2），绝不拒绝/改写。

        为什么要检查两处：pydantic-settings 只把 `Settings` 已有字段的键交给
        validator（env var 路在没有同名字段时根本不会出现在 `data` 里）；而 env file
        的残留值因为 `extra != "forbid"` 会进 `data`，却**不进** `os.environ`。两条路
        都是部署实态，缺一条就会漏报。

        为什么是 warning 而不是拒绝：见 `_RETIRED_KEYS` 上方注释——`extra="ignore"`
        的启动鲁棒性是既有政策，告警只提供可观察信号。

        `raiseExceptions` 那一小段（#1535 X-P2-2(e)）：`setup_logging` 在构造 handler
        时就把 `sys.stderr` 绑成当时的流，进程里换过/关过 stderr（pytest capsys 每轮
        换一个，上一轮结束时关闭；部署里重定向到已关的管道同理）后 root handler 仍
        指着死流。此时 emit 失败会让 stdlib `Handler.handleError` 往**当前** stderr 打
        `--- Logging error --- Traceback …`——那是 logging 自身的错误，不是配置错误，
        却会连坐调用方对 stderr 的断言。只在 root 上确有已关闭流时对**这一条** emit
        关掉 `raiseExceptions`（stdlib 文档语义：False = 忽略 logging 自身异常）；活
        handler（caplog / JSONL / 活 console）仍照收本记录，正常部署可观察性不变。

        ponytail: `logging.raiseExceptions` 是模块级全局，这一小段不是线程安全的——
        它只围住一次构造期 emit，且只在已检出死流时才短暂置 False。构造期是启动
        单线程路径；若将来 `Settings()` 在并发路径上高频构造，改成自带
        `handleError` 的 handler 子类（或在 `setup_logging` 侧剔除死 handler）。
        """
        present = {str(key).lower() for key in os.environ}
        if isinstance(data, Mapping):
            present.update(str(key).lower() for key in data)
        dead_stream = any(
            getattr(getattr(handler, "stream", None), "closed", False)
            for handler in logging.getLogger().handlers
        )
        raise_exceptions = logging.raiseExceptions
        if dead_stream:
            logging.raiseExceptions = False
        try:
            for retired in _RETIRED_KEYS:
                if retired.lower() in present:
                    logger.warning(
                        "%s 已退役（#1535），该键被忽略：检索目标只由 KNOWLEDGE_COLLECTION"
                        "（Settings.knowledge_collection）决定，请从部署配置中删除它。",
                        retired,
                    )
        finally:
            logging.raiseExceptions = raise_exceptions
        return data

    @field_validator("max_context_tokens")
    @classmethod
    def _validate_context_window_tier(cls, value: int) -> int:
        """上下文窗口在**构造期**（= 启动期）校验挡位，与同族 `Field(ge=1)` 同风格。

        挡位外的值在服务起来时即响亮失败，而不是拖到 run 跑到一半才在压缩路径抛
        （那时用户已经烧了 token，且报错落在一个「压缩失败」的壳里，看不出是配置
        写错了）。挡位清单单点在 `CONTEXT_WINDOW_TIERS`——API 的 PATCH 校验与前端
        选择器都读它。

        注意作用域：这是**进程级默认值**。per-session 覆盖走 `context/window.py` 的
        append-only 事件（#1197 项 1），不经过本校验器——挡位合法性在那里由同一个
        常量把关。
        """
        if value not in CONTEXT_WINDOW_TIERS:
            raise ValueError(
                f"max_context_tokens 必须是 {list(CONTEXT_WINDOW_TIERS)} 之一，得到 {value}"
            )
        return value

    @field_validator("ask_user_question_timeout")
    @classmethod
    def _validate_ask_user_question_timeout(cls, value: int) -> int:
        """`ask_user` 超时只允许票面三档（构造期即启动期，配错响亮失败）。"""
        if value not in ASK_USER_QUESTION_TIMEOUT_TIERS:
            raise ValueError(
                "ask_user_question_timeout must be one of "
                f"{sorted(ASK_USER_QUESTION_TIMEOUT_TIERS)}, got {value}"
            )
        return value

    @field_validator("model_endpoint_allowlist")
    @classmethod
    def _validate_endpoint_allowlist(cls, value: str) -> str:
        """#1485 端点例外在构造期（= 启动期）校验：配错就起不来，而不是静默永不命中。

        解析规则单点在 `model.endpoint_guard`（运行期读的是同一份字符串）；延迟
        import 与该模块的既有用法一致（`model/*` 反向依赖 `config`，顶部 import 会成环）。
        """
        from agent_harness.model.endpoint_guard import validate_allowlist

        validate_allowlist(value)
        return value

    @field_validator("attachment_allowed_media_types")
    @classmethod
    def _validate_allowed_media_types(cls, value: str) -> str:
        """附件允许类型在**构造期**（= 启动期）校验，与同族 `Field(ge=1)` 同风格。

        配置错误（未知类型 / 空）在服务起来时即响亮失败，而不是拖到每次上传才在
        请求路径抛 `ValueError`（那会变成 500，运维只能从请求日志发现配置错误）。
        解析规则单点在 `attachments.validation.parse_allowed_media_types`——该模块
        **不依赖 `Settings`**，故这里可以模块顶部直接 import（#939 M-15：此前为了
        躲开与 `attachments.types` 的 import 环，这里写的是函数体内延迟 import，
        而那道保护无机械守卫；解析规则搬进中性模块后方向单向，延迟 import 一并删除）。
        """
        parse_allowed_media_types(value)
        return value
