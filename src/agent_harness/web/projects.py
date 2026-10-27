"""项目（Workspace）HTTP 面——WS-4 / #154。

独立 router 文件（与 `lineage.py` 同款）：`app.py` 已被流式改造重刀过，注册只经
`create_app` 里的一行调用接入，把冲突面压到最小。

## 契约形状（前端消费）

```
Project = {id, path, title, status, session_ids, created_at, updated_at}
```

`session_ids` 是**账本手工序**（已过成员资格过滤），前端按它渲染项目内顺序；
`status` 只有 `ok` / `missing-dir`（目录被移走时**不改任何记录**，只如实上报）。

## 两条语义（票面硬约束）

1. **软删除**：`DELETE /api/projects/{id}` 只摘注册记录与账本——目录、用户文件、
   实时会话、已落盘日志一概不动，那些会话回到未分组。响应里的 `detail` 必须说清这一点
   （AC4：避免调用方误以为连坐删除）。
2. **对模型不可见**：项目是宿主侧能力，本模块**不写任何 `SessionEvent`**。

## 来源闸（ADR-0025 D1 的 (b)）

`create(path)` 接受**任意已存在目录**，这是本票新增的能力面；ADR-0025 D1 要求它在
未配置 `jwt_secret` 的本地信任模式 + 浏览器来源必须收紧下也必须有一条防线。现在由
中央入口统一校验精确 `Host` / `Origin`，所有项目端点（读 + 写）继续共享这份策略：
浏览器跨源请求带上非本机 hostname → 403。读端点也挂闸的理由是**答案本身是本地信息**——
项目列表里全是用户的绝对路径，不能让任意网页枚举。

无 `Origin` 的请求（CLI / curl / 服务端）不是浏览器发起，仍须先通过精确 Host 闸后放行；
配置了 `jwt_secret` 时整体跳过——那时请求已过认证层，跨源网页拿不到签名 token。
放行 no-Origin 意味着**部署前提仍是"服务只绑 loopback"**（见 ADR-0025 D1 残留段）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from agent_harness.sandbox.paths import is_absolute_path
from agent_harness.session.errors import SessionServiceError
from agent_harness.session.projects import ProjectService
from agent_harness.storage.sqlite import StorageBusyError
from agent_harness.web.app import project_service
from agent_harness.web.domain_errors import (
    http_error,
    storage_http_error,
    workspace_http_error,
)
from agent_harness.workspace import Workspace, WorkspaceError

if TYPE_CHECKING:
    from fastapi import FastAPI

#: 视为"本机来源"的 hostname（Vite dev server 5173 / 直连 8000 / IPv6 回环）。
_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_LOCAL_ORIGIN_SCHEMES = frozenset({"http", "https"})
_TEST_ASGI_HOSTNAME = "testserver"

# 只给 CORS 一个本机 hostname 的正则，而不是 ``*`` 或 ``*.localhost``：端口
# 由 Vite / 临时测试服务器选择，hostname 仍必须是一个精确的 loopback 名称。
LOCAL_CONTROL_PLANE_ORIGIN_REGEX = (
    r"(?i)^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::[1-9][0-9]{0,4})?$"
)


def _valid_port(value: str) -> int | None:
    if not value.isdigit():
        return None
    port = int(value)
    return port if 1 <= port <= 65535 else None


def _parse_host(value: str) -> tuple[str, str] | None:
    """解析 Host 为 ``(hostname, canonical_host_header)``，失败即 None。"""
    if not value or value != value.strip() or any(char.isspace() for char in value):
        return None
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            return None
        hostname = value[1:closing].lower()
        suffix = value[closing + 1:]
        if suffix:
            if not suffix.startswith(":"):
                return None
            port = _valid_port(suffix[1:])
            if port is None:
                return None
            suffix = f":{port}"
        canonical = f"[{hostname}]" + suffix
    else:
        if value.count(":") > 1:
            # IPv6 literals must use RFC 2732 brackets in Host.
            return None
        hostname, separator, raw_port = value.partition(":")
        hostname = hostname.lower()
        if separator:
            port = _valid_port(raw_port)
            if port is None:
                return None
            canonical = f"{hostname}:{port}"
        else:
            canonical = hostname
    if not hostname or any(char in hostname for char in "/\\@"):
        return None
    return hostname, canonical


def canonical_control_plane_origin(origin: str) -> str | None:
    """返回一个严格的 HTTP(S) origin；路径、凭据与畸形端口一律失败。"""
    if not origin or origin != origin.strip() or origin.casefold() == "null":
        return None
    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in _LOCAL_ORIGIN_SCHEMES
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (port is not None and _valid_port(str(port)) is None)
    ):
        return None
    # ``http://localhost:`` has no parsed port but is still malformed.
    if not parsed.netloc.rsplit("@", 1)[-1].endswith("]") and parsed.netloc.endswith(":"):
        return None
    hostname = hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    port_suffix = "" if port in (None, default_port) else f":{port}"
    return f"{parsed.scheme.lower()}://{host}{port_suffix}"


def configured_control_plane_origins(raw: str) -> frozenset[str]:
    """解析显式额外来源；不接受 ``*``、``null`` 或带路径的伪 origin。"""
    configured: set[str] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        canonical = canonical_control_plane_origin(value)
        if canonical is None:
            raise ValueError(f"invalid CONTROL_PLANE_ALLOWED_ORIGINS entry: {value!r}")
        configured.add(canonical)
    return frozenset(configured)


def configured_control_plane_hosts(raw: str) -> frozenset[str]:
    """解析显式 Host；拒绝含 ``*`` / ``null``，其余按完整 host[:port] 精确匹配。"""
    configured: set[str] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        if "*" in value or value.casefold() == "null":
            raise ValueError(f"invalid CONTROL_PLANE_ALLOWED_HOSTS entry: {value!r}")
        parsed = _parse_host(value)
        if parsed is None:
            raise ValueError(f"invalid CONTROL_PLANE_ALLOWED_HOSTS entry: {value!r}")
        configured.add(parsed[1])
    return frozenset(configured)


def origin_is_local(origin: str) -> bool:
    """该 `Origin` 是否算"本机来源"（HTTP 与 WS 两条面的**同一判据**，#890）。

    `Origin: null`（sandboxed iframe / `file://`）没有 hostname → 不算本机。
    `urlsplit`（`urlparse` 就是它加一段 params 拆分）对畸形 netloc（如 `http://[::1`
    少一个 `]`）抛 `ValueError` —— 那是**不可信输入**，按"不是本机"处理（#890 P3-5：
    不接住会冒泡成 500，把一次设计的 403 变成"服务器出错"，还给任意客户端一个选
    失败模式的手段）。判据方向不变，仍是 fail-closed。

    抽出来是为了让 WS 来源闸（`AuthSeamMiddleware._authenticate_websocket`）与
    下面的 `require_trusted_origin` 共用一份实现——两处各写一份必然漂移，
    而"WS 与 HTTP 同口径"正是 #890 的验收内容。
    """
    canonical = canonical_control_plane_origin(origin)
    if canonical is None:
        return False
    try:
        hostname = urlsplit(canonical).hostname
    except ValueError:
        return False
    return hostname is not None and hostname.lower() in _LOCAL_HOSTNAMES


def check_trusted_host(
    scope: object,
    *,
    allowed_hosts: Collection[str] = (),
    allow_testserver: bool = False,
) -> str | None:
    """中央 Host 闸：精确 loopback / 显式 Host，拒绝 DNS rebinding 形状。

    ``testserver`` is an explicit in-process test seam only; production callers leave
    ``allow_testserver`` false, so the real socket path never trusts that hostname.
    """
    raw_scope = scope if isinstance(scope, dict) else {}
    values = [
        value.decode("latin-1") if isinstance(value, bytes) else str(value)
        for name, value in raw_scope.get("headers", [])
        if (name.decode("latin-1") if isinstance(name, bytes) else str(name)).lower()
        == "host"
    ]
    raw_host = values[0] if len(values) == 1 else None
    parsed = _parse_host(raw_host) if raw_host is not None else None
    if parsed is None:
        return f"拒绝请求：Host 必须是本机回环或显式配置的精确值（Host={raw_host!r}）。"
    hostname, canonical = parsed
    server = raw_scope.get("server")
    server_hostname = (
        str(server[0]).lower()
        if isinstance(server, (tuple, list)) and server
        else ""
    )
    if (
        hostname not in _LOCAL_HOSTNAMES
        and canonical not in allowed_hosts
        and not (
            allow_testserver
            and hostname == _TEST_ASGI_HOSTNAME
            and server_hostname in {"", _TEST_ASGI_HOSTNAME}
        )
    ):
        return f"拒绝请求：Host 不是允许的本机值（Host={raw_host!r}）。"
    return None


def cross_origin_reason(origin: str) -> str:
    """跨源拒的**唯一文案**（HTTP 403 detail 与 WS 日志共用，防两处漂移）。

    只写"拒绝原因"，不含出口形状：HTTP 面把它放进 403 的 JSON `detail`，WS 面把它
    记进日志（close reason 另有固定短 ASCII 常量，ASGI 限 ≤123 字节可打印 ASCII）。

    ⚠ **不枚举端点**：`require_trusted_origin` 现在服务项目 / 目录列举 / 记忆 / 工作区文件 /
    WS 握手等多个面，写死"宿主侧 API（项目 / 目录列举）"会在一半调用点上是事实错误，
    也会把 HTTP 的端点词带进 WS 日志。只写这条闸自身的口径，措辞与调用面无关。
    """
    return (
        f"拒绝跨源访问：Origin={origin!r}。该接口只接受精确本机来源或显式配置来源"
        "（配置 JWT_SECRET 后由认证层接管）。"
    )


def check_trusted_origin(
    origin: str | None,
    *,
    jwt_secret_configured: bool,
    allowed_origins: Collection[str] = (),
) -> str | None:
    """来源闸的**策略本体**（HTTP 与 WS 共用一份，#890 P3-4）。

    返回 `None` = 放行；返回字符串 = 拒绝原因（即 `(ok, reason)` 里的 `reason`）。
    判定顺序与常量都只此一处：

    1. 配了 `jwt_secret` ⇒ 放行（认证层才是边界，见 `AuthSeamMiddleware`）；
    2. 无 `Origin` ⇒ 放行（非浏览器发起：第三方网页**无法**构造不带 Origin 的
       浏览器请求/握手，故这条不构成 drive-by 面）；
    3. 否则只接受精确的本机 HTTP(S) origin，或显式配置的完整 origin。

    两侧只在**出口形状**上分叉：HTTP 把它转成 403 JSON，WS 把它转成握手前的
    `websocket.close`。策略与文案只有这一份（`WS_ORIGIN_DENIED_REASON` /
    `WS_CREDENTIAL_REJECTED_REASON` 是 WS 出口专有的**短 ASCII** close reason，
    ASGI 限 ≤123 字节，与这里的长文案用途不同，仍留在 `app.py` 各一份）。
    """
    if jwt_secret_configured or origin is None:
        return None
    canonical = canonical_control_plane_origin(origin)
    if canonical is not None and (
        origin_is_local(origin) or canonical in allowed_origins
    ):
        return None
    return cross_origin_reason(origin)


def require_trusted_origin(request: Request) -> None:
    """宿主侧端点的来源闸（ADR-0025 D1 的 (b)；ADR-0028 D2 起 `GET /api/host/dirs` 复用同一份）。

    只在未配置 `jwt_secret`（本地信任模式）时生效；无 `Origin`、本机 `Origin` 或
    显式配置的来源放行。
    `Origin: null`（sandboxed iframe / `file://`）没有 hostname → 拒绝。
    策略正文在 `check_trusted_origin`（与 WS 面共用），此处只做出口形状。
    """
    state = request.app.state.agent
    reason = check_trusted_origin(
        request.headers.get("origin"),
        jwt_secret_configured=bool(state.settings.jwt_secret),
        allowed_origins=getattr(request.app.state, "allowed_origins", ()),
    )
    if reason is not None:
        raise HTTPException(status_code=403, detail=reason)


def _require_absolute_path(value: str) -> str:
    """项目路径必须是**绝对路径**（ADR-0025 D1：前端让用户输入绝对路径）。

    为什么不能只靠 `canonical_workspace_path` 兜：`realpath` 会把 `""` / `"   "` / `"."`
    解析成**进程当前工作目录**、把 `".."` 解析成**盘根**——于是"注册我的项目"变成"把服务器
    碰巧启动的目录、甚至整个盘当成项目"。这是 WS-1 已就 `realpath("")` 记录过的同一类
    **静默锚定**错误，必须在规范化**之前**挡住形态。

    文案（API-02，2026-09-13 真机验收）：三句都走**中文**，与同层的
    `host_dirs._directory_listing`（`path 必须是绝对路径` / `path 含非法字符（NUL）`）和
    `SessionService._resolve_cwd`（`cwd 必须是绝对路径：…`）同一口径。此前这里是英文
    （`path must be an absolute path`），而前端 `readErrorDetail()` 只剥掉 Pydantic 的
    `Value error, ` 前缀、把 msg 原样渲染——中文界面里直接蹦出一句英文。
    改的是**我们自己写**的 msg（不是 Pydantic 内建文案），所以不引入全局 localizer。
    """
    if not value.strip():
        raise ValueError("path 不能为空")
    if "\x00" in value:
        raise ValueError("path 含非法字符（NUL）")
    # 平台分支的绝对形态判定收在 `sandbox.paths.is_absolute_path`（#170 批次抽出）：
    # 此处原先写 `PureWindowsPath(value).is_absolute()`，在 POSIX 上会把合法绝对路径
    # （`/home/x`，无 drive → False）一律拒掉；Windows 口径逐字不变（仍要求盘符 + 根）。
    if not is_absolute_path(value):
        raise ValueError("path 必须是绝对路径")
    return value


class Project(BaseModel):
    """单个项目（`GET /api/projects` 一行）。"""

    id: str
    path: str
    title: str
    #: 目录当前是否存在；`missing-dir` 只上报事实，不改注册记录（AC8 的同类立场）。
    status: Literal["ok", "missing-dir"]
    #: 账本**手工序**（已过成员资格过滤）——前端按它渲染项目内顺序。
    session_ids: list[str]
    created_at: str
    updated_at: str


class CreateProjectRequest(BaseModel):
    """`POST /api/projects`：注册一个**已存在**的目录（不存在的路径 → 404，不 mkdir）。"""

    path: str = Field(min_length=1, max_length=4096)
    title: str | None = Field(default=None, max_length=200)

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        return _require_absolute_path(value)

    @field_validator("title")
    @classmethod
    def _reject_blank_title(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            # 同 API-02 口径：我们自己写的 msg 一律中文（前端原样渲染 msg）。
            raise ValueError("title 不能为空")
        return value


class ResolveProjectRequest(BaseModel):
    """`POST /api/projects/resolve`：按路径解析，**不注册**。"""

    path: str = Field(min_length=1, max_length=4096)

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        return _require_absolute_path(value)


class RenameProjectRequest(BaseModel):
    """`PATCH /api/projects/{id}`（`setTitle`）。"""

    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def _reject_blank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title 不能为空")
        return value


class AttachSessionRequest(BaseModel):
    """`POST /api/projects/{id}/sessions`：把会话加入项目（幂等）。"""

    session_id: str = Field(min_length=1)


class ReorderSessionRequest(BaseModel):
    """`POST /api/projects/{id}/sessions/{sid}/order`：账本内重排。

    `before=None` → 追加尾部（DOM `insertBefore` 语义）。
    """

    before: str | None = None


class ProjectCreated(Project):
    """`POST /api/projects` 的响应 = 项目 + 本次新归入的会话数（AC5 / #169）。

    为什么要多这个字段：注册是"确保这个目录下的会话都归这儿"的动作，调用方需要知道
    它**实际**改动了什么（软删除 → 重注册后补回 N 个；幂等重放为 0），而不是自己去
    比对注册前后的账本——那正是"前端猜后端语义"。
    """

    #: 本次调用新 attach 进来的会话数（幂等重放 → 0）。
    sessions_attached: int


class ProjectDeleted(BaseModel):
    """`DELETE /api/projects/{id}` 的响应：**软删除**结果。

    `detail` 必须让调用方一眼看到"会话没被删除"（AC4）——前端可直接显示它。
    """

    id: str
    deleted: bool
    #: 有多少会话因此回到未分组。
    sessions_detached: int
    detail: str


@asynccontextmanager
async def _translated() -> AsyncIterator[None]:
    """把领域异常翻成 HTTP（两套词汇 → 两张 single-source 表）。

    这里用上下文管理器而不是每端点两臂 `except`：本模块有 9 个端点，
    逐个重述就是 ARCH-5 刚消灭掉的 18 个 except 臂（#144 的结论）。

    元组里的 `OSError` 是**父类**——`FileNotFoundError` / `NotADirectoryError` /
    `PermissionError` / 裸 `OSError`（EINVAL、ENAMETOOLONG）都由 `_WORKSPACE_ERROR_STATUS`
    按**精确类型**分别给出 404 / 422 / 403 / 422。本模块的 OSError 来源只有路径校验
    （`os.stat` / `realpath`）；SQLite 走 aiosqlite 抛 `sqlite3.Error`（不是 OSError），
    所以不会把内部 I/O 故障伪装成 4xx。
    """
    try:
        yield
    except SessionServiceError as error:  # 会话层词汇（含 WS-4 的 WorkspaceMoveInvalid）
        raise http_error(error) from error
    except (WorkspaceError, OSError) as error:
        raise workspace_http_error(error) from error
    except StorageBusyError as error:
        # #515：workspace store 写锁竞争在重试预算内未缓解 → 503（与 archive 等
        # 端点同款；审查 P2-3 补齐——本模块此前漏映射，裸 500）。
        raise storage_http_error(error) from error


def _project(workspace: Workspace) -> Project:
    return Project(
        id=workspace.id,
        path=workspace.path,
        title=workspace.title,
        status=workspace.status(),
        session_ids=list(workspace.session_ids),
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


def register_project_routes(app: FastAPI) -> None:
    """把项目路由挂到既有 app（`create_app` 里一行调用的接入面）。"""

    def _service() -> ProjectService:
        return project_service(app.state.agent)

    @app.post("/api/projects")
    async def create_project(
        req: CreateProjectRequest, _: None = Depends(require_trusted_origin)
    ) -> ProjectCreated:
        """注册**已存在**的目录为项目，并归入 cwd 匹配的既有会话（AC5 / #169）。

        同一规范路径 → 返回既有实体（不重建、不重复入序，AC5）；路径不存在 → 404、
        存在但不是目录 → 422（AC2/AC3）；两种情况都**不**在磁盘上留下痕迹。
        响应里的 `sessions_attached` = 本次**新**归入的会话数（幂等重放 → 0）。
        """
        async with _translated():
            project, attached = await _service().create(req.path, req.title)
        return ProjectCreated(
            **_project(project).model_dump(), sessions_attached=attached
        )

    @app.get("/api/projects")
    async def list_projects(_: None = Depends(require_trusted_origin)) -> list[Project]:
        """全部项目，**注册表顺序**（新建项目前插，不是按标题/路径排序）。

        读端点也过来源闸：响应里是用户的绝对路径（见模块 docstring）。
        """
        async with _translated():
            return [_project(w) for w in await _service().list()]

    @app.post("/api/projects/resolve")
    async def resolve_project(
        req: ResolveProjectRequest, _: None = Depends(require_trusted_origin)
    ) -> Project:
        """按路径解析项目（幂等查询，**不注册**）；未注册 → 404。

        前端"这个目录是不是已有项目"的入口：拿到 404 才去调 create。
        """
        async with _translated():
            return _project(await _service().resolve(req.path))

    @app.get("/api/projects/{project_id}")
    async def get_project(
        project_id: str, _: None = Depends(require_trusted_origin)
    ) -> Project:
        async with _translated():
            return _project(await _service().get(project_id))

    @app.patch("/api/projects/{project_id}")
    async def rename_project(
        project_id: str,
        req: RenameProjectRequest,
        _: None = Depends(require_trusted_origin),
    ) -> Project:
        """重命名（`setTitle`）——标题是项目唯一的可变元数据。"""
        async with _translated():
            return _project(await _service().rename(project_id, req.title))

    @app.delete("/api/projects/{project_id}")
    async def delete_project(
        project_id: str, _: None = Depends(require_trusted_origin)
    ) -> ProjectDeleted:
        """**软删除**：只摘注册记录与账本，会话与目录一概不动（AC4/AC11）。"""
        async with _translated():
            project, detached = await _service().delete(project_id)
        return ProjectDeleted(
            id=project.id,
            deleted=True,
            sessions_detached=detached,
            detail=(
                f"项目「{project.title}」已从注册表移除，{detached} 个会话回到未分组。"
                "目录、用户文件与会话日志均未删除（软删除，可重新注册同一目录）。"
            ),
        )

    @app.post("/api/projects/{project_id}/sessions")
    async def attach_session(
        project_id: str,
        req: AttachSessionRequest,
        _: None = Depends(require_trusted_origin),
    ) -> Project:
        """把会话加入项目（AC7：会话必须已存在、已带 cwd，且 cwd 指向本项目）。"""
        async with _translated():
            return _project(await _service().attach(project_id, req.session_id))

    @app.delete("/api/projects/{project_id}/sessions/{session_id}")
    async def detach_session(
        project_id: str,
        session_id: str,
        _: None = Depends(require_trusted_origin),
    ) -> Project:
        """把会话移出项目（幂等：不在本项目 → 无写操作）；会话日志逐字节不动。"""
        async with _translated():
            return _project(await _service().detach(project_id, session_id))

    @app.post("/api/projects/{project_id}/sessions/{session_id}/order")
    async def reorder_session(
        project_id: str,
        session_id: str,
        req: ReorderSessionRequest,
        _: None = Depends(require_trusted_origin),
    ) -> Project:
        """账本内重排（`before=None` → 追加尾部）；跨项目重排 → 409。"""
        async with _translated():
            return _project(
                await _service().reorder(project_id, session_id, req.before)
            )
