"""SKILL.md 发现与解析（spec 09 §2，Pi PORT DESIGN，ADR-0011 Q1/Q2/Q4）。

渐进披露的物理前提：发现阶段只读 frontmatter（name + description），
正文通过 load_body() 按需读取。解析失败显式进 errors 列表，不静默跳过。

路径边界：目录扫描发现的 SKILL.md 经 resolve 后必须仍落在被扫描目录内
（防 symlink 指向目录外）；手动指定的路径是用户显式声明，本身即授权。
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from agent_harness.skills.package_lock import MANIFEST_FILENAME, registry_lock

#: 发现阶段单文件读取上限（stat 尺寸拦截，先于任何 read；单位是字节）。
#: SKILL.md 是被扫描目录里的自由文件（模型 workspace-write 可写），无上限时
#: 超大文件会在 wiring 期打爆内存——tool 层的 64K 正文截断发生在完整读盘之后，
#: 拦不住读入阶段。
SKILL_FILE_MAX_BYTES = 1_000_000

#: 写入侧单文件上限（#529 §5.1，PORT DESIGN 依据 B：oh-my-pi MAX_MANAGED_SKILL_BYTES）：
#: register/update 序列化出的**整个** SKILL.md 字节上限。与发现侧 SKILL_FILE_MAX_BYTES
#: 语义不同、不合并——那边是"读进来最多多大"（读入阶段有界性），这边是"写出去最多
#: 多大"（沉淀产物有界，防单条 skill 吃掉整个 Context 预算）。
MAX_SKILL_BYTES = 64_000

#: name 白名单（#588）：小写字母/数字开头，其后可含连字符/下划线（对齐本仓已注册
#: 工具名惯例：load_skill / retrieve_knowledge；MCP `_NAME_PATTERN` 同族），换行、
#: 控制符、空白、大写全在集合外。64 上限与 Pi（skills.ts MAX_NAME_LENGTH）和
#: Agent Skills 规范一致。方案依据：#588 issue 评论（协议 §1.3，Pi + agentskills.io）。
SKILL_NAME_MAX_LENGTH = 64
_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-_]*")

#: 目录行 / ToolResult message 是**单行声明面**（#588 兜底层）。
#: #607/F2：补 U+0085 NEL / U+2028 LS / U+2029 PS——str.splitlines() 识别集的
#: 这三成员（CPython 文档全集，本机 3.13.5 实测）漏掉则「单行」输出仍被撕成多行。
_LINE_BREAKERS = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]+")


def single_line(text: str) -> str:
    """把换行与控制符压成空格，保证插值点不被字段内容拉成多行。

    入口白名单只覆盖 name（解析期拒绝）；description / when_to_use 是自由文本，
    load_skill 失败路的 `args.name` 更是**模型原始输入**（未名即失败、不经
    discovery）——插值前在此单行化。是兜底，不替代入口校验。
    """
    return _LINE_BREAKERS.sub(" ", text)


def _spath(path: str | os.PathLike[str]) -> str:
    """路径插值单行化（#607/F3）：POSIX 文件名可合法含换行/控制符，错误串不得
    被路径内容拉成多行（Pi skills.ts 对 filePath 同样做 escapeXml 纪律）。"""
    return single_line(str(path))


def _is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_point)


@dataclass(frozen=True, slots=True)
class SkillCatalogEntry:
    """目录条目：name + description(+when_to_use) + 来源路径 + frontmatter 其余字段；正文延迟读取。"""

    name: str
    description: str
    source_path: Path
    meta: dict[str, Any] = field(default_factory=dict)
    when_to_use: str = ""  # 可选（ADR-0011 grill 记录：用户批准的扩展字段）
    # 目录扫描来源的包含根（手动路径为 None：用户显式声明本身即授权）。
    # load_body 读盘时用它重验证边界——发现时的一次性校验是 TOCTOU：
    # 模型可通过 workspace-write 工具在 wiring 之后把 skill 目录换成
    # 指向目录外的 junction/symlink，把任意宿主文件读进 Context。
    scanned_root: Path | None = None
    # Verify the exact bytes consumed by lazy load_skill against the install snapshot.
    managed_skill_sha256: str | None = None

    def load_body(self) -> str:
        """按需读取 SKILL.md 正文（frontmatter 之后的部分）——每次读盘，不缓存。"""
        if self.scanned_root is not None and not resolve_within(self.source_path, self.scanned_root):
            raise OSError(
                f"{_spath(self.source_path)}: resolves outside scanned skill directory "
                f"{_spath(self.scanned_root)} (boundary changed after discovery, refusing to read)"
            )
        # 尺寸上限在 load 侧同样生效：发现后文件可被 workspace-write 换成超大内容，
        # "读入阶段有界"必须覆盖模型触发的加载路径（与发现同一威胁模型）。
        # Bound the read itself: a post-discovery replacement must not turn the stat
        # check into an unbounded allocation.
        with self.source_path.open("rb") as handle:
            raw = handle.read(SKILL_FILE_MAX_BYTES + 1)
        if len(raw) > SKILL_FILE_MAX_BYTES:
            raise OSError(f"{_spath(self.source_path)}: too large (> {SKILL_FILE_MAX_BYTES} bytes)")
        if (
            self.managed_skill_sha256 is not None
            and hashlib.sha256(raw).hexdigest() != self.managed_skill_sha256
        ):
            raise OSError(
                f"{_spath(self.source_path)}: changed after installation; refusing to load"
            )
        # utf-8-sig：Windows 记事本等默认写 BOM，残留 \ufeff 会让首行 '---' 校验失败。
        # 非 UTF-8 字节抛 UnicodeDecodeError（ValueError 子类）——让调用方明确看到
        # 读盘失败，而不是吞回空字符串（吞空会让 load_skill 工具返回空内容）。
        text = raw.decode("utf-8-sig")
        return _split_frontmatter(text)[1].strip()


@dataclass
class SkillCatalog:
    """发现结果：条目 + 显式错误 + 冲突标注（不静默）。"""

    entries: list[SkillCatalogEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def _split_frontmatter(text: str) -> tuple[str | None, str]:
    """切 `---` 围栏 frontmatter；返回 (frontmatter_text | None, body)。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1:])
    return None, text


def parse_skill_markdown(path: Path) -> tuple[SkillCatalogEntry | None, list[str]]:
    """解析单个 SKILL.md；失败返回 (None, errors)，绝不抛出中断发现流程。"""
    try:
        # 尺寸上限先于 read（stat 一次 vs 全量读入）：读入阶段就有界。
        if path.stat().st_size > SKILL_FILE_MAX_BYTES:
            return None, [f"{_spath(path)}: too large (> {SKILL_FILE_MAX_BYTES} bytes)"]
        # utf-8-sig：兼容 BOM 前缀（Windows 记事本默认），无 BOM 时行为与 utf-8 一致。
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        # UnicodeDecodeError 是 ValueError 子类，不是 OSError——非 UTF-8（GBK/Latin-1
        # 等）字节会从这里抛；捕获它才不违背"解析失败进 errors，绝不中断扫描"的契约。
        return None, [f"{_spath(path)}: unreadable ({type(error).__name__})"]
    return parse_skill_markdown_text(text, path)


def parse_skill_markdown_text(
    text: str, path: Path
) -> tuple[SkillCatalogEntry | None, list[str]]:
    """解析已经有界读取的 Skill 文本，复用发现路径的同一 frontmatter 规则。"""
    errors: list[str] = []
    frontmatter, _body = _split_frontmatter(text)
    if frontmatter is None:
        return None, [f"{_spath(path)}: missing '---' frontmatter fence"]
    try:
        meta = yaml.safe_load(frontmatter)
    except yaml.YAMLError as error:
        return None, [f"{_spath(path)}: invalid YAML frontmatter ({type(error).__name__})"]
    if not isinstance(meta, dict):
        return None, [f"{_spath(path)}: frontmatter must be a mapping"]
    meta = dict(meta)
    name = meta.pop("name", None)
    description = meta.pop("description", None)
    when_to_use = meta.pop("when_to_use", None)
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{_spath(path)}: frontmatter requires non-empty 'name'")
    elif not _NAME_PATTERN.fullmatch(name) or len(name) > SKILL_NAME_MAX_LENGTH:
        # #588：多行/控制符 name 会插值进 ToolResult message 与 catalog 目录行，
        # 把单行声明拉成多行、可伪造后续行的"系统语气"——入口整体拒绝，进 errors
        # 可观察（ADR-0011 Q1 同款通道）。name 走 repr、path 走 _spath 单行化，错误行自身保持单行。
        errors.append(
            f"{_spath(path)}: invalid skill name (must match ^[a-z0-9][a-z0-9-_]*$, "
            f"max {SKILL_NAME_MAX_LENGTH} chars, single line; got {name!r})"
        )
    if not isinstance(description, str) or not description.strip():
        errors.append(f"{_spath(path)}: frontmatter requires non-empty 'description'")
    if errors:
        return None, errors
    when_to_use = when_to_use.strip() if isinstance(when_to_use, str) and when_to_use.strip() else ""
    return SkillCatalogEntry(name=name.strip(), description=description.strip(),
                             source_path=path, meta=meta, when_to_use=when_to_use), []


def resolve_within(candidate: Path, root: Path) -> bool:
    """resolve 后必须仍在 root 内（含相等）——目录扫描的 symlink 逃逸防线。"""
    resolved = candidate.resolve()
    root = root.resolve()
    return resolved == root or root in resolved.parents


def serialize_skill_markdown(entry: SkillCatalogEntry) -> str:
    """把目录条目序列化回标准 SKILL.md（frontmatter + 正文）。

    frontmatter 经 yaml.safe_dump 生成（meta 里的任意字段值可往返）；
    正文取 entry.load_body()——源文件不可读时在此抛出，半字未写。
    """
    meta: dict[str, Any] = {"name": entry.name, "description": entry.description}
    if entry.when_to_use:
        meta["when_to_use"] = entry.when_to_use
    meta.update(entry.meta)
    frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    return f"---\n{frontmatter}---\n\n{entry.load_body().strip()}\n"


class _ManagedClaim(NamedTuple):
    """受管根的归属声明：命中的安装期正文摘要；None = 命中受管根但未被本项目选中。"""

    digest: str | None


class SkillDiscovery:
    """扫描 skill 目录（只一层 `skills/<name>/SKILL.md`）+ 手动指定路径。

    #529：本类同时是闭环的写入面（register/update/remove）——只写 project
    skill 目录（``<workspace>/skills/<name>/SKILL.md``）；global 目录仅由用户
    手动维护，闭环写入显式拒绝（未配置 project_dir 即无写入面）。
    """

    def __init__(
        self,
        directories: list[Path],
        manual_paths: list[Path] | None = None,
        project_dir: Path | None = None,
        *,
        managed_directories: dict[str, Path] | None = None,
        enabled_managed_digests: dict[str, dict[str, str]] | None = None,
        selection_errors: list[str] | None = None,
    ) -> None:
        self._directories = [Path(d) for d in directories]
        self._manual_paths = [Path(p) for p in (manual_paths or [])]
        # 写入目标 = project skill 目录。None = 未配置写入面：register/update/remove
        # 响亮拒绝，绝不落 global（避免污染用户全局）。
        self._project_dir = Path(project_dir) if project_dir is not None else None
        # #874 T5：托管根按 scope 各有一个（project 挂在 <workspace>/skills 下、
        # global 挂在全局安装根下）；每个根只暴露**本项目显式启用**的那个快照。
        self._managed_directories = {
            scope: Path(root) for scope, root in (managed_directories or {}).items()
        }
        # 装配期读启用结果失败（例如本项目选中的全局版本失效）不能只留一行日志：
        # spec 08 §6.3 要求管理状态在可观察面留痕，否则「被选版本失效」与「本来
        # 就没选」在程序上不可区分。这条错误随每次 discover() 进 catalog.errors，
        # 由 SkillCapability.errors() / Web 只读面呈现（T5 AC3）。
        self._selection_errors = [
            f"[selection] {message}" for message in (selection_errors or [])
        ]
        self._enabled_managed_digests = {
            scope: dict(digests)
            for scope, digests in (enabled_managed_digests or {}).items()
        }
        # 最近一次 discover() 结果的缓存：SkillCapability 持本类引用（不再是装配期
        # 静态 catalog），写入方法刷新后 capability 的可见性随之收敛。
        self._catalog: SkillCatalog | None = None

    @property
    def project_dir(self) -> Path | None:
        """闭环写入面（project skill 目录）；None = 未配置写入面。

        更新分支（§3-4/§10-5）需要用它判定"既有同名条目是否是闭环可更新的
        合法目标"（global/manual 来源不可更新——闭环不写 global）。
        """
        return self._project_dir

    def discover(self) -> SkillCatalog:
        catalog = SkillCatalog(errors=list(self._selection_errors))
        seen: dict[str, tuple[Path, bool]] = {}
        blocked_managed_conflicts: set[str] = set()

        def _consider(path: Path, origin: str, root: Path | None) -> None:
            claim = self._managed_claim_for(path)
            if claim is not None and claim.digest is None:
                return
            managed_digest = claim.digest if claim is not None else None
            entry, errors = parse_skill_markdown(path)
            catalog.errors.extend(f"[{origin}] {e}" for e in errors)
            if entry is None:
                return
            if root is not None and not resolve_within(path, root):
                catalog.errors.append(
                    f"[{origin}] {_spath(path)}: resolves outside scanned skill directory {_spath(root)}"
                )
                return
            if root is not None or managed_digest is not None:
                # 携带包含根，供 load_body 读盘时重验证（TOCTOU 防线）。
                entry = replace(
                    entry,
                    scanned_root=root,
                    managed_skill_sha256=managed_digest,
                )
            if entry.name in blocked_managed_conflicts:
                return
            if entry.name in seen:
                previous_path, previous_managed = seen[entry.name]
                # 同名先到先得，冲突显式可见（spec 08 §5 精神：不允许静默忽略）。
                catalog.conflicts.append(
                    f"skill '{entry.name}' from {_spath(path)} shadowed by {_spath(previous_path)}"
                )
                if previous_managed or managed_digest is not None:
                    # A configured source can appear after an imported Skill was selected.
                    # Do not expose either copy until the name conflict is resolved.
                    catalog.entries = [item for item in catalog.entries if item.name != entry.name]
                    seen.pop(entry.name, None)
                    blocked_managed_conflicts.add(entry.name)
                return
            seen[entry.name] = (path, managed_digest is not None)
            catalog.entries.append(entry)

        for directory in self._directories:
            managed_scope = self._managed_scope_for(directory)
            if not directory.exists():
                continue  # 目录不存在 → 空 catalog，不是错误（OPTIONAL 语义）
            if not directory.is_dir():
                catalog.errors.append(f"[directory] {_spath(directory)}: not a directory")
                continue
            if managed_scope is not None and not self._managed_directory_is_safe(directory):
                catalog.errors.append(f"[directory] {_spath(directory)}: managed directory is unsafe")
                continue
            try:
                skill_dirs = sorted(directory.iterdir())
            except OSError as error:
                # 目录级 IO 错误（权限/死挂载）只损失该目录：与逐条解析失败同一
                # 容错契约，绝不中断整个扫描（否则一个坏目录让所有技能消失）。
                catalog.errors.append(f"[directory] {_spath(directory)}: unreadable ({error})")
                continue
            for skill_dir in skill_dirs:
                if managed_scope is not None and skill_dir.name not in self._enabled_digests(managed_scope):
                    continue
                skill_file = skill_dir / "SKILL.md"
                if skill_dir.is_dir() and skill_file.is_file():
                    _consider(
                        skill_file,
                        "directory",
                        skill_dir if managed_scope is not None else directory,
                    )

        for manual in self._manual_paths:
            if manual.is_file():
                _consider(manual, "manual", None)
            else:
                catalog.errors.append(f"[manual] {_spath(manual)}: not a file")
        self._catalog = catalog
        return catalog

    def _managed_roots(self) -> list[tuple[str, Path]]:
        """(scope, 托管根) 列表；按 scope 名排序，扫描顺序确定（与平台无关）。"""
        return sorted(self._managed_directories.items())

    def _managed_scope_for(self, directory: Path) -> str | None:
        """该扫描目录是不是受管根；是则返回它的 scope。"""
        for scope, root in self._managed_roots():
            if directory == root:
                return scope
        return None

    def _enabled_digests(self, scope: str) -> dict[str, str]:
        """该 scope 里本项目显式启用、且安装期摘要已知的包名 → 正文摘要。"""
        return self._enabled_managed_digests.get(scope, {})

    def _managed_directory_is_safe(self, root: Path) -> bool:
        """Keep a managed package root below the real directory that owns it.

        project：root 是 ``<workspace>/skills/.managed``，要求它真的挂在
        ``<workspace>/skills`` 下。global：root 是全局安装根的 ``skills/.managed``
        （安装根本身已与自动发现根分离，见 wiring），同样只要求父子关系与解析不逃逸。
        """
        drop_in = root.parent
        try:
            if _is_reparse_point(root) or _is_reparse_point(drop_in):
                return False
            drop_in_root = drop_in.resolve(strict=True)
            managed_root = root.resolve(strict=True)
            if managed_root.parent != drop_in_root:
                return False
            install_root = drop_in_root.parent
            return install_root != drop_in_root and install_root.is_dir()
        except (OSError, RuntimeError):
            return False

    def _managed_claim_for(self, path: Path) -> _ManagedClaim | None:
        """受管归属声明；未受管返回 None。

        `digest` 为 None 表示「路径确实落在某个受管根里，但不是本次装配选中的那个
        快照」——调用方据此丢弃该条目（受管根不得绕过本项目保存的启用结果）。
        用具名字段而不是裸元组：`(scope, digest)` 的 scope 位无人消费，写成
        `if claim:` 之类会把它误当成"已选中"放行。
        """
        for scope, root in self._managed_roots():
            try:
                path.absolute().relative_to(root.absolute())
                lexical_candidate = True
            except ValueError:
                lexical_candidate = False
            try:
                managed_root = root.resolve(strict=True)
                relative = path.resolve(strict=True).relative_to(managed_root)
                resolved_candidate = True
            except (OSError, RuntimeError, ValueError):
                relative = None
                resolved_candidate = False
            if not lexical_candidate and not resolved_candidate:
                continue
            if not self._managed_directory_is_safe(root) or relative is None:
                return _ManagedClaim(None)
            if (
                len(relative.parts) != 2
                or relative.parts[1].casefold() != "skill.md"
                or _is_reparse_point(root / relative.parts[0])
                or _is_reparse_point(path)
            ):
                return _ManagedClaim(None)
            name = relative.parts[0]
            digest = self._enabled_digests(scope).get(name)
            return _ManagedClaim(digest)
        return None

    # ── #529：当前目录（缓存投影）+ 闭环写入路径 ──────────────────────────────

    def catalog(self) -> SkillCatalog:
        """最近一次发现结果；从未 discover 过时先扫一遍（wiring 装配即调用）。"""
        if self._catalog is None:
            return self.discover()
        return self._catalog

    def register(self, entry: SkillCatalogEntry) -> None:
        """把草稿条目写入 project skill 目录并刷新 registry（#529 §6.1 一等接缝）。

        写文件 + 重新 discover() 同事务：任一失败整体报错、已写文件回滚。
        oh-my-pi 的血泪教训是 learn 提升不刷新 skill registry →"写了但不可见"；
        这里把刷新内嵌进写入方法，调用方不存在"忘了刷"的分离调用面。
        """
        self._write_entry(entry)

    def update(self, name: str, entry: SkillCatalogEntry) -> None:
        """更新既有 skill：整文件重写（模型重写全文路线，#529 §10-5），语义同 register。"""
        if name != entry.name:
            raise ValueError(f"update name mismatch: {name!r} != entry name {entry.name!r}")
        self._write_entry(entry)

    def remove(self, name: str) -> None:
        """删除 project 目录里的 skill 并刷新；global/手动路径来源的 skill 显式拒绝。"""
        if self._project_dir is None:
            raise ValueError("refusing to remove a Skill without a project skill directory")
        with registry_lock(self._project_dir.parent / MANIFEST_FILENAME):
            target = next((e for e in self.discover().entries if e.name == name), None)
            if target is None:
                raise ValueError(f"skill '{name}' is not in the catalog")
            if not resolve_within(target.source_path, self._project_dir):
                # 文件即真相：不是 project 目录里的文件，就不归闭环写路径管。
                raise ValueError(
                    f"refusing to remove '{name}': source {_spath(target.source_path)} "
                    f"is outside project skill directory"
                )
            skill_file = target.source_path
            skill_file.unlink()
            try:
                skill_file.parent.rmdir()  # 空目录顺手清掉；非空（用户放了别的文件）则保留
            except OSError:
                pass
            self._refresh(skill_file)

    def _write_entry(self, entry: SkillCatalogEntry) -> Path:
        """序列化 + 落盘 + 刷新（同事务）。任何一步失败不留半写状态。"""
        # 纵深防御（审查 P2）：entry.name 是磁盘路径的组成部分——解析期白名单
        # （#588）在这里同样强制，公开写入 API 的任何调用方（不限于 promoter 的
        # 解析路径）都不能让 `../x` 式 name 落到 project 目录之外。
        if not _NAME_PATTERN.fullmatch(entry.name) or len(entry.name) > SKILL_NAME_MAX_LENGTH:
            raise ValueError(
                f"refusing to write: invalid skill name {entry.name!r} "
                f"(must match ^[a-z0-9][a-z0-9-_]*$, max {SKILL_NAME_MAX_LENGTH} chars)"
            )
        if self._project_dir is None:
            raise ValueError(
                "refusing to write: no project skill directory configured "
                "(global skill directory is user-maintained and never written by the loop)"
            )
        # 序列化先于任何落盘：源不可读 / 超限在此抛出，磁盘半字未动。
        content = serialize_skill_markdown(entry)
        if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
            raise ValueError(
                f"skill '{entry.name}': serialized SKILL.md too large "
                f"({len(content.encode('utf-8'))} > {MAX_SKILL_BYTES} bytes)"
            )
        with registry_lock(self._project_dir.parent / MANIFEST_FILENAME):
            skill_dir = self._project_dir / entry.name
            skill_file = skill_dir / "SKILL.md"
            current = next(
                (item for item in self.discover().entries if item.name == entry.name),
                None,
            )
            project_update = (
                current is not None
                and resolve_within(current.source_path, self._project_dir)
                and current.source_path.resolve() == skill_file.resolve()
            )
            if skill_dir.exists() and not project_update:
                raise ValueError(
                    f"Skill '{entry.name}' already exists; refusing to overwrite or shadow it"
                )
            for _scope, managed_root in self._managed_roots():
                managed_target = managed_root / entry.name
                if managed_target.exists() or managed_target.is_symlink():
                    raise ValueError(
                        f"managed Skill '{entry.name}' already exists; refusing a name conflict"
                    )
            if current is not None and not project_update:
                raise ValueError(
                    f"Skill '{entry.name}' is already provided outside the project directory"
                )
            created = not skill_dir.exists()
            if created:
                skill_dir.mkdir(parents=True)
            try:
                skill_file.write_text(content, encoding="utf-8")
            except OSError:
                if created:
                    skill_dir.rmdir()  # 尽力清理本次新建的空壳目录（无半写）
                raise
            self._refresh(skill_file)
            return skill_file

    def _refresh(self, *written: Path) -> None:
        """写入后刷新 catalog（重新 discover）；失败整体报错并回滚本次写入的文件。"""
        try:
            self.discover()
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            raise
