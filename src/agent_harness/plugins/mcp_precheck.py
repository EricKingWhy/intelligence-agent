"""MCP server 描述预检（T6 / #875）：静态解析 → 映射到现有 `MCPServerConfig`。

预检的本质是**静态分析描述文件**：只读包内声明，不启动子进程、不连远端、不读
部署环境、不回显凭据值。描述只有落在本仓 `MCPServerConfig` 能表达的字段上才算被
接受（spec 09 §1.1）；包里没有明确描述时报告"需要适配描述"，不去猜 package.json、
README 或脚本里的命令（ADR-0052 D2、AC1）。

来源（一手）：
- MCP 2025-11-25 Transports：标准 transport 只有 stdio 与 Streamable HTTP。stdio 由
  客户端起子进程、凭据走进程环境；Streamable HTTP 是**单一 endpoint URL**；HTTP+SSE
  是 2024-11-05 的弃用形态。⇒ 独立 `sse` 形状在本仓 `MCPServerConfig`（只有 stdio/http）
  里没有对应字段，必须明确失败而不是静默降级成 http。
- MCP 2025-11-25 Authorization：OAuth 对实现**可选**；HTTP transport SHOULD 遵从，
  stdio SHOULD NOT 而改为从环境取凭据；客户端在 401/403 上才发起授权。⇒ 需要登录的
  server 在本仓无法以匿名方式使用，只能显式标 `oauth_required_unsupported`（AC4）。
- Claude Code MCP 文档（code.claude.com/docs/en/mcp）：`.mcp.json` 的
  `{"mcpServers": {name: {...}}}` 形状、stdio 的 `type`/`command`/`args`/`env`、
  http 的 `type`/`url`/`headers`、`streamable-http` 是 `http` 的别名、`sse` 已弃用
  （"The SSE transport is deprecated"）、`oauth` 对象即"需要登录"（用 `/mcp` 授权）、
  无 `type` 的 url 条目会被它自己当 stdio（"a url entry without a type fails"）、
  `timeout` 单位是毫秒、`headersHelper` 是每次连接现跑一个脚本取头。
- 本机 venv 里的 MCP Python SDK 2.1.1：`mcp/client/stdio.py:93` 的
  `StdioServerParameters`（command/args/env/cwd）与 `mcp/cli/claude.py:107` 读
  `mcpServers` 键——SDK 侧**不做静态 schema 校验**（未知键要到启动时才炸）⇒ 本仓
  沿用 ADR-0012 的 strict pydantic 校验，未知字段响亮失败（AC3）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, get_args

from agent_harness.mcp.config import MCPServerConfig
from agent_harness.redaction import redact_secret_values
from agent_harness.skills.inspection import _add_error, read_package_text
from agent_harness.tooling.contract import ToolPermission

#: 显式 server 描述文件（相对包根，顺序即优先序）。
MCP_DESCRIPTION_FILES = ("mcp.json", ".mcp.json", "mcp-servers.json", "mcpServers.json")

#: 两种形状的包装键：Claude Code 是 `mcpServers` 对象，本仓是 `servers` 列表。
_CLAUDE_WRAPPER = "mcpServers"
_NATIVE_WRAPPER = "servers"

#: Claude Code 的 `type` → 本仓 transport。`streamable-http` 是它文档里的 `http` 别名；
#: `sse`/`ws`/`sdk` 本仓无表达（None ⇒ 明确失败，见 `_claude_transport`）。
_CLAUDE_TRANSPORTS: dict[str, str | None] = {
    "stdio": "stdio",
    "http": "http",
    "streamable-http": "http",
    "sse": None,
    "ws": None,
    "sdk": None,
}

#: 两种形状下本仓 `MCPServerConfig` 会读的键。其余键要么是别家的形状、要么是写错的
#: 字段名——两种都不能静默丢弃（ADR-0012）。`resources`/`prompts` 是**声明面**：
#: 本仓无对应字段，但由 `_capability_gaps` 逐项给出缺口，不在这里重复报。
#: 本仓形状的合法键**就是模型自己的字段**——预检比模型严，就等于拒绝一份
#: `MCPServerConfig` 明明能表达的描述（AC1 的边界是"能否表达"，不是"我们喜不喜欢"）。
_NATIVE_FIELDS = frozenset(MCPServerConfig.model_fields)

#: 本仓支持的 transport 直接取自模型的 `Literal`（写死一份就等于多一个会漂的真相）。
_NATIVE_TRANSPORTS = frozenset(get_args(MCPServerConfig.model_fields["transport"].annotation))

#: Claude Code 形状的合法键按 transports 分别列（其文档里 stdio 用 command/args/env，
#: http 用 url/headers；`env` 在 http 上虽无意义但无害，照样接受并只列出变量名）。
#: `oauth`/`resources`/`prompts` 不在这里出结论——它们各自由显式缺口检查处理。
_CLAUDE_FIELDS = {
    "stdio": frozenset(
        {"type", "command", "args", "env", "cwd", "timeout", "enabled", "tool_permissions", "resources", "prompts"}
    ),
    "http": frozenset(
        {"type", "url", "headers", "env", "timeout", "enabled", "tool_permissions", "oauth", "resources", "prompts"}
    ),
}

#: HTTP header 名的合法形态（RFC 7230 token）。
_HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+\Z")

#: 未展开的 `${...}`（含 `${VAR}` / `${VAR:-default}`）。
_SECRET_REF = re.compile(r"\$\{[^}]*\}")

#: `${VAR}` / `${VAR:-default}` 的引用名与默认值（与本仓 config.py 的 `_SECRET_REF` 同形）。
_SECRET_NAME = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

#: 相对形态（`./`、`../`、`.\`、`..\`）：出现即按"包内路径"判，而不是当作裸命令名。
_RELATIVE_PATH = re.compile(r"\.{1,2}[/\\]")

#: 裸命令名的形状（真正"在不在 PATH"是部署事实，静态判不了、也不猜）。
_COMMAND_SHAPE = re.compile(r"[A-Za-z0-9_.+-]+\Z")


def inspect_mcp_servers(source: Path | str, *, root: Path | str | None = None) -> dict[str, Any]:
    """预检一个包里的 MCP server 描述；返回报告，绝不启动 server / 展开凭据。

    `root` 是已解析的包根（`plugins inspect` 与 Skill 预检共用同一个根与同一套边界
    读写）；缺省时用 `source` 自身解析。任何情况下都不执行包内代码。
    """
    source_path = Path(source).expanduser()
    report: dict[str, Any] = {
        "status": "needs-adaptation",
        "source": str(source_path),
        "description_file": None,
        "servers": [],
        "requirements": [],
        "errors": [],
    }
    errors: list[dict[str, str]] = report["errors"]
    try:
        package_root = Path(root).resolve(strict=True) if root is not None else source_path.resolve(strict=True)
    except (OSError, RuntimeError):
        _add_error(errors, "MCP_PACKAGE_NOT_FOUND", ".", "包目录不存在或无法解析。")
        report["status"] = "unsupported"
        return report
    report["source"] = str(package_root)

    found = _find_description(package_root, errors)
    if found is None:
        # 没有描述 ≠ 坏包：这是"需要一份薄适配描述"的正常结论（AC1），不是错误。
        report["requirements"].append(
            {
                "kind": "mcp-description",
                "name": " / ".join(MCP_DESCRIPTION_FILES),
                "support": "needs-adaptation",
                "reason": (
                    "包里没有显式 MCP server 描述。请补一份声明 server 的描述文件；"
                    "预检不从 package.json、README 或任意脚本猜启动命令（ADR-0052 D2）。"
                ),
            }
        )
        return report

    relative, text = found
    report["description_file"] = relative
    raw = _parse_description(text, relative, errors)
    if raw is None:
        report["status"] = "unsupported"
        return report

    claude_shape = _is_claude_shape(raw)
    entries = _server_entries(raw, relative, errors)
    if entries is None:
        report["status"] = "unsupported"
        return report

    seen: set[str] = set()
    for name, body in entries:
        if name in seen:
            _add_error(
                errors,
                "MCP_SERVER_NAME_DUPLICATE",
                relative,
                f"server 名重复: {name!r}（同名先到先得会静默吞掉后者）",
            )
            continue
        seen.add(name)
        report["servers"].append(
            _inspect_entry(package_root, relative, name, body, claude_shape=claude_shape)
        )

    if errors:
        report["status"] = "unsupported"
    elif not report["servers"]:
        # 描述文件在，但一个 server 都没有：不是一个可用的描述（AC1 的否定面）。
        _add_error(errors, "MCP_DESCRIPTION_EMPTY", relative, "描述里没有任何 server 条目。")
        report["status"] = "unsupported"
    elif any(server["status"] != "supported" for server in report["servers"]):
        report["status"] = "needs-adaptation"
    else:
        report["status"] = "complete"
    return report


def _find_description(root: Path, errors: list[dict[str, str]]) -> tuple[str, str] | None:
    """按固定顺序找第一个显式描述文件；读取走包内边界读写（越界/过大/变化都记错误）。"""
    for relative in MCP_DESCRIPTION_FILES:
        candidate = root / relative
        if not candidate.exists() and not candidate.is_symlink():
            continue
        text = read_package_text(
            root,
            relative,
            errors,
            too_large_code="MCP_DESCRIPTION_TOO_LARGE",
            unreadable_code="MCP_DESCRIPTION_UNREADABLE",
            changed_code="MCP_DESCRIPTION_CHANGED",
            not_file_code="MCP_DESCRIPTION_NOT_A_FILE",
            outside_code="MCP_DESCRIPTION_OUTSIDE_PACKAGE",
        )
        if text is not None:
            return relative, text
    return None


def _parse_description(text: str, relative: str, errors: list[dict[str, str]]) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as error:
        _add_error(errors, "MCP_DESCRIPTION_INVALID", relative, f"JSON 无法解析: {error}")
        return None
    if not isinstance(parsed, dict):
        _add_error(
            errors,
            "MCP_DESCRIPTION_INVALID",
            relative,
            f"描述必须是 JSON 对象，得到 {type(parsed).__name__}",
        )
        return None
    _check_duplicate_keys(text, parsed, relative, errors)
    return parsed


def _check_duplicate_keys(
    text: str, parsed: dict[str, Any], relative: str, errors: list[dict[str, str]]
) -> None:
    """JSON 里重复的键 → 明确失败。

    `json.loads` 对重复键**静默取后者**：`{"mcpServers": {"a": {...}, "a": {...}}}`
    会不声不响地少一个 server（AC3 的"重复名"在 Claude 形状里唯一的出现方式）。
    先按 object_pairs_hook 收一遍重复键，再按它是不是 server 名给出对应的错误码。
    """
    duplicates: list[str] = []

    def _hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        return dict(pairs)

    try:
        json.loads(text, object_pairs_hook=_hook)
    except (json.JSONDecodeError, ValueError):  # pragma: no cover - 上面已解析成功过
        return
    if not duplicates:
        return
    wrapper = parsed.get(_CLAUDE_WRAPPER)
    server_names = set(wrapper) if isinstance(wrapper, dict) else set()
    for key in sorted(set(duplicates)):
        if key in server_names:
            _add_error(
                errors,
                "MCP_SERVER_NAME_DUPLICATE",
                relative,
                f"server 名重复: {key!r}（JSON 重复键会被静默取后者，等于少一个 server）",
            )
        else:
            _add_error(
                errors,
                "MCP_DESCRIPTION_DUPLICATE_KEY",
                relative,
                f"描述里键 {key!r} 重复（JSON 重复键语义不明确，一律拒绝）",
            )


def _is_claude_shape(raw: dict[str, Any]) -> bool:
    return isinstance(raw.get(_CLAUDE_WRAPPER), dict)


def _server_entries(
    raw: dict[str, Any], relative: str, errors: list[dict[str, str]]
) -> list[tuple[str, dict[str, Any]]] | None:
    """归一成 `[(name, entry)]`；两种形状以外一律明确失败，不做形状猜测。"""
    claude = raw.get(_CLAUDE_WRAPPER)
    native = raw.get(_NATIVE_WRAPPER)
    if claude is not None and native is not None:
        _add_error(
            errors,
            "MCP_DESCRIPTION_INVALID",
            relative,
            f"同时出现 {_CLAUDE_WRAPPER!r} 与 {_NATIVE_WRAPPER!r}；一次只接受一种形状",
        )
        return None
    if isinstance(claude, dict):
        entries: list[tuple[str, dict[str, Any]]] = []
        for name, body in claude.items():
            if not isinstance(body, dict):
                _add_error(
                    errors,
                    "MCP_SERVER_INVALID",
                    relative,
                    f"server {name!r} 必须是对象，得到 {type(body).__name__}",
                )
                continue
            entries.append((str(name), body))
        return entries
    if isinstance(native, list):
        entries = []
        for index, body in enumerate(native):
            if not isinstance(body, dict):
                _add_error(
                    errors,
                    "MCP_SERVER_INVALID",
                    relative,
                    f"{_NATIVE_WRAPPER}[{index}] 必须是对象，得到 {type(body).__name__}",
                )
                continue
            name = body.get("name")
            if not isinstance(name, str) or not name:
                _add_error(
                    errors,
                    "MCP_SERVER_INVALID",
                    relative,
                    f"{_NATIVE_WRAPPER}[{index}] 缺少字符串 name（本仓以 name 字段为键）",
                )
                continue
            entries.append((name, body))
        return entries
    _add_error(
        errors,
        "MCP_DESCRIPTION_INVALID",
        relative,
        f"描述必须含 {_CLAUDE_WRAPPER} 对象（Claude Code 形状）或 {_NATIVE_WRAPPER} 列表（本仓形状）",
    )
    return None


def _inspect_entry(
    package_root: Path, relative: str, name: str, entry: dict[str, Any], *, claude_shape: bool
) -> dict[str, Any]:
    """单个 server 条目的静态预检；每条判定都落成报告字段。"""
    report: dict[str, Any] = {
        "name": name,
        "status": "supported",
        "transport": None,
        "command": None,
        "args": [],
        "url": None,
        "cwd": None,
        "environment": [],
        "headers": [],
        "timeout_seconds": None,
        "enabled": True,
        "tool_permissions": [],
        "dependencies": [],
        "requirements": [],
        "errors": [],
    }
    failures: list[dict[str, str]] = report["errors"]
    requirements: list[dict[str, str]] = report["requirements"]

    def fail(code: str, path: str, message: str) -> None:
        _add_error(failures, code, f"{name}.{path}" if path else name, message)

    def gap(kind: str, support: str, name_of_item: str, reason: str) -> None:
        requirements.append({"kind": kind, "name": name_of_item, "support": support, "reason": reason})

    transport = (
        _claude_transport(entry, name, fail) if claude_shape else _native_transport(entry, name, fail)
    )
    if transport is None:
        report["status"] = "unsupported"
        return report
    report["transport"] = transport

    if transport == "stdio":
        _check_stdio(package_root, entry, name, fail, gap, report, relative)
    else:
        _check_http(entry, name, fail, gap, report)
        if claude_shape:
            _check_oauth(entry, name, gap)
        _check_headers(entry, name, fail, gap, report)

    _check_capability_gaps(entry, name, gap)
    _check_unknown_fields(entry, name, claude_shape, fail, report)
    _check_tool_permissions(entry, name, fail, report)
    _check_environment(entry, name, fail, gap, report)
    _check_timeout(entry, name, fail, report)
    report["enabled"] = entry.get("enabled", True) is not False

    if failures:
        report["status"] = "unsupported"
    elif any(item["support"] != "supported" for item in requirements):
        report["status"] = "needs-adaptation"
    return report


def _claude_transport(entry: dict[str, Any], server: str, fail) -> str | None:
    """Claude Code 形状：`type` 决定 transport；无 type 而带 url 是它自己会失败的写法。"""
    declared = entry.get("type")
    if declared is None:
        if "url" in entry:
            fail(
                "MCP_TRANSPORT_UNSUPPORTED",
                "type",
                f"server {server!r} 有 url 但没有 type；无 type 的条目被当作 stdio，会失败"
                "（请显式写 type: \"http\"）",
            )
            return None
        declared = "stdio"
    if not isinstance(declared, str):
        fail("MCP_SERVER_INVALID", "type", f"server {server!r} 的 type 必须是字符串")
        return None
    if declared not in _CLAUDE_TRANSPORTS:
        fail(
            "MCP_TRANSPORT_UNSUPPORTED",
            "type",
            f"server {server!r} 的 type {declared!r} 不是本仓支持的 transport"
            f"（支持: stdio / http / streamable-http）",
        )
        return None
    mapped = _CLAUDE_TRANSPORTS[declared]
    if mapped is None:
        fail(
            "MCP_TRANSPORT_UNSUPPORTED",
            "type",
            f"server {server!r} 的 transport {declared!r} 不被支持：本仓只有 stdio 与"
            " Streamable HTTP（http）；MCP 2025-11-25 已用 Streamable HTTP 取代 HTTP+SSE",
        )
    return mapped


def _native_transport(entry: dict[str, Any], server: str, fail) -> str | None:
    """本仓形状：直接吃 `MCPServerConfig.transport`（即 `parse_mcp_servers` 的输入）。"""
    declared = entry.get("transport")
    if not isinstance(declared, str):
        fail("MCP_TRANSPORT_UNSUPPORTED", "transport", f"server {server!r} 缺少字符串 transport")
        return None
    if declared not in _NATIVE_TRANSPORTS:
        fail(
            "MCP_TRANSPORT_UNSUPPORTED",
            "transport",
            f"server {server!r} 的 transport {declared!r} 不被支持（只有 stdio / http）",
        )
        return None
    return declared


def _check_stdio(package_root, entry, server, fail, gap, report, relative) -> None:
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        fail("MCP_COMMAND_MISSING", "command", f"stdio server {server!r} 缺少非空 command")
        return
    args = entry.get("args", [])
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        fail("MCP_SERVER_INVALID", "args", f"server {server!r} 的 args 必须是字符串列表")
        return
    cwd = entry.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        fail("MCP_SERVER_INVALID", "cwd", f"server {server!r} 的 cwd 必须是非空字符串")
        return
    if isinstance(cwd, str):
        _resolve_package_path(package_root, cwd.strip(), "cwd", server, fail, report)
    report["args"] = list(args)
    executable = _resolve_package_path(package_root, command.strip(), "command", server, fail, report)
    if executable is None:
        return
    if report["command"] is None:
        report["command"] = command.strip()
    report["dependencies"].append(
        {
            "kind": "command",
            "name": executable,
            "source": relative,
            "support": "manual_review",
            "reason": "外部命令是否可用由部署环境决定；本预检不执行、也不查 PATH。",
        }
    )
    unexpanded = set(_unexpanded_refs(command)) | {
        ref for item in args for ref in _unexpanded_refs(item)
    }
    if unexpanded:
        fail(
            "MCP_SECRET_REFERENCE_INVALID",
            "command",
            f"server {server!r} 的 command/args 含无法展开的引用: {sorted(unexpanded)}"
            "（本仓只展开 env/headers 的值）",
        )
        return
    for value in (command, *args):
        if _SECRET_REF.search(value):
            gap(
                "env-var",
                "manual_review",
                value,
                "该值在运行时由部署环境提供；本预检不注入、不读取进程环境。",
            )


def _check_http(entry, server, fail, gap, report) -> None:
    url = entry.get("url")
    if not isinstance(url, str) or not url.strip():
        fail("MCP_URL_MISSING", "url", f"http server {server!r} 缺少非空 url")
        return
    url = url.strip()
    report["url"] = url
    refs = _unexpanded_refs(url)
    for var in sorted(refs):
        gap(
            "env-var",
            "manual_review",
            var,
            "URL 里的部署环境变量引用在运行时展开；本预检只列出引用名，不取值。",
        )


def _check_oauth(entry, server, gap) -> None:
    """Claude Code 形状的 `oauth` 对象 = 该 server 需要登录（`claude mcp login`）。"""
    if "oauth" not in entry:
        return
    gap(
        "oauth_required_unsupported",
        "unsupported",
        server,
        "该 server 声明需要 OAuth 凭据；本仓首版不实现 OAuth 登录，不能以完整兼容启用"
        "（spec 09 §1.1、ADR-0052 D2）。",
    )


def _check_headers(entry, server, fail, gap, report) -> None:
    headers = entry.get("headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
    ):
        fail("MCP_SERVER_INVALID", "headers", f"server {server!r} 的 headers 必须是字符串到字符串的对象")
        return
    for key, value in headers.items():
        if not _HEADER_NAME.fullmatch(key):
            fail("MCP_HEADER_NAME_INVALID", "headers", f"server {server!r} 的 header 名 {key!r} 非法")
            continue
        # header 值不回显（`Authorization: Bearer <token>` 之类），只留键名与引用形状。
        report["headers"].append({"name": key, "value": _value_shape(value)})
        _check_secret_value(server, f"headers.{key}", value, fail, gap)


def _check_capability_gaps(entry, server, gap) -> None:
    """这个包声明要用到的 MCP 原语，逐项对照本仓支持面（AC4）。"""
    for primitive, detail in (
        ("resources", "本仓 MCP 只实现 tools 原语；resources 未实现，该 server 的这部分能力不可用。"),
        ("prompts", "本仓 MCP 只实现 tools 原语；prompts 未实现，该 server 的这部分能力不可用。"),
    ):
        declared = entry.get(primitive)
        if declared is True or (isinstance(declared, (list, dict)) and declared):
            gap(primitive, "unsupported", f"{server}:{primitive}", detail)


def _check_unknown_fields(entry, server, claude_shape, fail, report) -> None:
    declared = report["transport"] or "stdio"
    allowed = _CLAUDE_FIELDS[declared] if claude_shape else _NATIVE_FIELDS
    unknown = sorted(set(entry) - allowed)
    if unknown:
        fail(
            "MCP_FIELD_UNKNOWN",
            "",
            f"server {server!r} 含本仓无法表达的字段 {unknown}：未知字段响亮失败，不静默丢弃"
            "（配置写错是人的错误，必须立刻看见）。本仓可表达: "
            f"{sorted(allowed)}",
        )


def _check_tool_permissions(entry, server, fail, report) -> None:
    declared = entry.get("tool_permissions")
    if declared is None:
        return
    if not isinstance(declared, dict):
        fail("MCP_SERVER_INVALID", "tool_permissions", f"server {server!r} 的 tool_permissions 必须是对象")
        return
    for tool_name, value in declared.items():
        try:
            permission = ToolPermission(value)
        except ValueError:
            fail(
                "MCP_TOOL_PERMISSION_INVALID",
                "tool_permissions",
                f"server {server!r} 工具 {tool_name!r} 的权限 {value!r} 不是合法值"
                f"（{[item.value for item in ToolPermission]}）",
            )
            continue
        report["tool_permissions"].append({"tool": str(tool_name), "permission": permission.value})


def _check_environment(entry, server, fail, gap, report) -> None:
    """env 的**值**永不回显；只留变量名与"是 ${VAR} 引用还是明文"。"""
    env = entry.get("env", {})
    if not isinstance(env, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
    ):
        fail("MCP_SERVER_INVALID", "env", f"server {server!r} 的 env 必须是字符串到字符串的对象")
        return
    if not env:
        return
    for key, value in env.items():
        report["environment"].append({"name": key, "value": _value_shape(value)})
        _check_secret_value(server, f"env.{key}", value, fail, gap)


def _check_secret_value(server, label, value, fail, gap) -> None:
    """`${VAR}` 引用按名登记；写法非法（空引用/非法名/嵌套）明确失败。"""
    refs = _unexpanded_refs(value)
    if not refs:
        # 明文常量：不回显内容，也不因此判不兼容（AC2 只要求不回显）。
        if _SECRET_REF.search(value):
            fail(
                "MCP_SECRET_REFERENCE_INVALID",
                label,
                f"引用写法非法（空引用/非法变量名/嵌套）：{redact_secret_values(value)!r}",
            )
        return
    if "${" in _SECRET_REF.sub("", value):
        fail(
            "MCP_SECRET_REFERENCE_INVALID",
            label,
            f"引用写法非法（空引用/非法变量名/嵌套）：{redact_secret_values(value)!r}",
        )
        return
    for var in sorted(refs):
        gap(
            "env-var",
            "manual_review",
            var,
            f"{label} 引用了进程环境变量 {var}；本预检不读取部署环境，"
            "展开与缺失判定发生在运行时（ADR-0012）。",
        )


def _check_timeout(entry, server, fail, report) -> None:
    value = entry.get("timeout_seconds")
    if value is None and "timeout" in entry:
        # Claude Code 的 `timeout` 单位是毫秒（其文档明示），本仓记秒。
        declared = entry["timeout"]
        if isinstance(declared, bool) or not isinstance(declared, (int, float)):
            fail("MCP_SERVER_INVALID", "timeout", f"server {server!r} 的 timeout 必须是数字（毫秒）")
            return
        value = declared / 1000
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        fail("MCP_SERVER_INVALID", "timeout_seconds", f"server {server!r} 的 timeout 必须是正数")
        return
    report["timeout_seconds"] = float(value)


def _value_shape(value: str) -> dict[str, Any]:
    """值 → 可安全回显的形状描述（引用名 / 是否有默认值 / 是否明文），不含值本身。"""
    refs = _unexpanded_refs(value)
    if not refs:
        return {"kind": "literal"}
    return {
        "kind": "reference",
        "references": [
            {"name": var, "has_default": default is not None} for var, default in sorted(refs.items())
        ],
    }


def _unexpanded_refs(value: str) -> dict[str, str | None]:
    """`${VAR}` / `${VAR:-default}` 的引用名 → 默认值（None = 无默认）。"""
    return {match.group(1): match.group(2) for match in _SECRET_NAME.finditer(value)}


def _resolve_package_path(package_root, value: str, field: str, server: str, fail, report) -> str | None:
    """包内路径字段（command/cwd）的静态边界判定；返回报告要写的可执行名。

    含 `${VAR}` 的值静态判不了（那是部署环境解出来的路径），交给运行时；
    **只读**：只 `resolve()` 比较，不执行、也不跟随越界 symlink。
    """
    if not _SECRET_REF.search(value):
        looks_like_path = bool(_RELATIVE_PATH.match(value)) or "/" in value or "\\" in value
        if looks_like_path:
            candidate = Path(value)
            resolved = candidate.resolve() if candidate.is_absolute() else (package_root / candidate).resolve()
            if not _path_within(resolved, package_root):
                fail(
                    "MCP_PATH_OUTSIDE_PACKAGE",
                    field,
                    f"{field} 指向包外路径（{value!r}）；包只允许引用自身内容",
                )
                return None
            if not resolved.exists():
                fail("MCP_PATH_MISSING", field, f"{field} 指向的包内路径不存在: {value!r}")
                return None
            if field == "cwd":
                if not resolved.is_dir():
                    fail("MCP_PATH_NOT_DIRECTORY", field, f"{field} 指向的不是目录: {value!r}")
                    return None
                report["cwd"] = _package_label(resolved, package_root)
                return resolved.name
            # 可执行位只在 POSIX 上判：Windows 没有这个位，os.access(X_OK) 恒真。
            if os.name == "posix" and not os.access(resolved, os.X_OK):
                fail("MCP_PATH_NOT_EXECUTABLE", field, f"{field} 指向的文件不可执行: {value!r}")
                return None
            report["command"] = _package_label(resolved, package_root)
            return resolved.name
        if not _COMMAND_SHAPE.fullmatch(value):
            fail("MCP_COMMAND_UNUSABLE", field, f"{field} 的值不像可执行命令: {value!r}")
            return None
    return Path(value).name


def _path_within(path: Path, root: Path) -> bool:
    """`Path.is_relative_to` 的兜底形态（与 skills.inspection._within 同一语义）。"""
    return path == root or root in path.parents


def _package_label(resolved: Path, package_root: Path) -> str:
    """报告里只出现包内相对标签，不泄漏预检机的绝对路径。"""
    return f"${{PACKAGE}}/{resolved.relative_to(package_root).as_posix()}"
