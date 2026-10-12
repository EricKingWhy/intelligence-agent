from __future__ import annotations

import base64
import ctypes
import fnmatch
import json
import ntpath
import os
import stat
import struct
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4

from agent_harness.sandbox.capabilities import (
    probe_windows_psec_capabilities,
    require_available,
)
from agent_harness.sandbox.local import LocalSubprocessSandbox

_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_OPEN_IF = 3
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
_FILE_OPEN_REPARSE_POINT = 0x00200000
_OBJ_CASE_INSENSITIVE = 0x00000040
_OBJ_DONT_REPARSE = 0x00001000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_FILE_READ_DATA = 0x00000001
_FILE_WRITE_DATA = 0x00000002
_FILE_ADD_FILE = 0x00000002
_FILE_ADD_SUBDIRECTORY = 0x00000004
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_WRITE_ATTRIBUTES = 0x00000100
_FILE_TRAVERSE = 0x00000020
_FILE_LIST_DIRECTORY = 0x00000001
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_STATUS_REPARSE_POINT_ENCOUNTERED = 0xC000050B
_STATUS_NO_MORE_FILES = 0x80000006


class WorkspaceIdentityError(RuntimeError):
    """A persisted Windows workspace path no longer names the same directory."""


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", wintypes.LPVOID), ("Information", ctypes.c_size_t)]


class WindowsPsecSandbox(LocalSubprocessSandbox):
    """MXC PSEC child execution with Codex-style handle-relative workspace I/O."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        workspace_identity: dict[str, str] | None = None,
        max_capture_chars: int = 2_000_000,
        env_allowlist: tuple[str, ...] | None = None,
        passthrough_env: bool = False,
        fallback_encoding: str | None = None,
        mxc_executable: str | os.PathLike[str] | None = None,
    ) -> None:
        if os.name != "nt":
            raise RuntimeError("windows_psec sandbox is available only on Windows")
        raw_root = ntpath.abspath(os.fspath(workspace_root))
        self._workspace_identity = None
        self._expected_workspace_identity = workspace_identity
        self._pinned_handles: list[int] = []
        self._root_guard = threading.RLock()
        self._mxc_executable = (
            os.fspath(mxc_executable)
            if mxc_executable
            else os.environ.get("IA_MXC_EXECUTABLE")
        )
        self._initialize(
            Path(raw_root),
            max_capture_chars=max_capture_chars,
            env_allowlist=env_allowlist,
            passthrough_env=passthrough_env,
            fallback_encoding=fallback_encoding,
        )

    @property
    def workspace_identity(self) -> dict[str, str]:
        self.ensure_started()
        assert self._workspace_identity is not None
        return dict(self._workspace_identity)

    def ensure_started(self) -> None:
        with self._root_guard:
            if self._pinned_handles:
                return
            handles = _open_directory_chain(self._workspace_root, create=True)
            try:
                identity = _file_identity(handles[-1])
                expected = self._expected_workspace_identity
                if expected is not None and identity != expected:
                    raise WorkspaceIdentityError(
                        "Persisted workspace path resolves to a different Windows directory; "
                        "refusing recovery."
                    )
                temp = _open_relative_directory(
                    handles[-1],
                    ".tmp",
                    create=True,
                )
                _close_handles([temp])
                self._ensure_psec_available()
            except BaseException:
                _close_handles(handles)
                raise
            self._pinned_handles = handles
            self._workspace_identity = identity

    def stop(self) -> None:
        with self._root_guard:
            _close_handles(self._pinned_handles)
            self._pinned_handles.clear()

    def delete(self) -> None:
        with self._root_guard:
            handles = self._pinned_handles
            if not handles:
                try:
                    handles = _open_directory_chain(self._workspace_root, create=False)
                except FileNotFoundError:
                    return
            identity = _file_identity(handles[-1])
            if (
                self._expected_workspace_identity is not None
                and identity != self._expected_workspace_identity
            ):
                if handles is not self._pinned_handles:
                    _close_handles(handles)
                raise WorkspaceIdentityError(
                    "Persisted workspace path resolves to a different Windows directory; "
                    "refusing deletion."
                )
            delete_handle = None
            try:
                if len(handles) < 2:
                    raise ValueError(
                        "cannot delete a workspace directly under a volume root"
                    )
                components = _relative_components(
                    ntpath.basename(os.fspath(self._workspace_root)),
                )
                delete_handle = _open_relative_entry(
                    handles[-2],
                    components[-1],
                    desired_access=(
                        _DELETE
                        | _FILE_READ_ATTRIBUTES
                        | _FILE_LIST_DIRECTORY
                        | _FILE_WRITE_ATTRIBUTES
                        | _SYNCHRONIZE
                    ),
                    create_disposition=_FILE_OPEN,
                    create_options=_FILE_DIRECTORY_FILE
                    | _FILE_SYNCHRONOUS_IO_NONALERT
                    | _FILE_OPEN_FOR_BACKUP_INTENT,
                    share_access=(_FILE_SHARE_READ | _FILE_SHARE_WRITE),
                )
                if _file_identity(delete_handle) != identity:
                    raise WorkspaceIdentityError(
                        "Workspace path changed before deletion; refusing to delete."
                    )
                _delete_directory_contents(delete_handle)
                _delete_handle(delete_handle, clear_readonly=True)
            finally:
                if delete_handle is not None:
                    _close_handles([delete_handle])
                _close_handles(handles)
                self._pinned_handles = []
                self._workspace_identity = None

    def resolve_within_workspace(self, path: str) -> Path:
        """Validate a lexical relative path; actual I/O always reopens by handle."""
        components = _relative_components(path, allow_empty=True)
        return self._workspace_root.joinpath(*components)

    def path_kind(self, path: str) -> str | None:
        """Return file/directory from an object opened relative to the pinned root."""
        components = _relative_components(path, allow_empty=True)
        if not components:
            return "directory"
        parent, name, handles = self._open_parent(path)
        handle = None
        try:
            handle = _open_relative_entry(
                parent,
                name,
                desired_access=_FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                create_disposition=_FILE_OPEN,
                create_options=_FILE_SYNCHRONOUS_IO_NONALERT
                | _FILE_OPEN_FOR_BACKUP_INTENT,
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            )
            return "directory" if _file_standard_info(handle).Directory else "file"
        except FileNotFoundError:
            return None
        finally:
            if handle is not None:
                _close_handles([handle])
            _close_handles(handles)

    def list_files(self, pattern: str) -> list[str]:
        components = _relative_components(pattern, allow_empty=True, allow_glob=True)
        normalized = "/".join(components)
        effective = normalized or "*"
        results: list[str] = []
        self._collect_files(self._root_handle(), "", results)
        return sorted(path for path in results if _glob_match(path, effective))

    def _collect_files(self, directory: int, prefix: str, results: list[str]) -> None:
        for name in _directory_entry_names(directory):
            try:
                _relative_components(name)
                entry = _open_relative_entry(
                    directory,
                    name,
                    desired_access=(
                        _FILE_READ_ATTRIBUTES | _FILE_LIST_DIRECTORY | _SYNCHRONIZE
                    ),
                    create_disposition=_FILE_OPEN,
                    create_options=_FILE_SYNCHRONOUS_IO_NONALERT
                    | _FILE_OPEN_FOR_BACKUP_INTENT,
                    share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    allow_reparse_leaf=True,
                )
            except (FileNotFoundError, PermissionError):
                continue
            try:
                if _file_attributes(entry) & _FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                rel_path = f"{prefix}/{name}" if prefix else name
                if _file_standard_info(entry).Directory:
                    self._collect_files(entry, rel_path, results)
                else:
                    results.append(rel_path)
            finally:
                _close_handles([entry])

    def copy_in(self, host_path: Path, workspace_path: str) -> None:
        source = Path(host_path)
        source_info = source.lstat()
        if stat.S_ISLNK(source_info.st_mode):
            raise PermissionError("copy_in does not follow host reparse points")
        if stat.S_ISDIR(source_info.st_mode):
            parent, name, handles = self._open_parent(
                workspace_path,
                create=True,
                access_extra=_FILE_ADD_SUBDIRECTORY | _FILE_ADD_FILE,
            )
            destination = None
            try:
                destination = _create_relative_directory(parent, name)
                self._copy_host_directory(source, destination, workspace_path)
            finally:
                if destination is not None:
                    _close_handles([destination])
                _close_handles(handles)
            return
        if not stat.S_ISREG(source_info.st_mode):
            raise PermissionError("copy_in accepts only regular files and directories")
        with source.open("rb") as stream:
            self._write_stream(workspace_path, stream)

    def _copy_host_directory(
        self,
        source: Path,
        destination: int,
        workspace_path: str,
    ) -> None:
        with os.scandir(source) as entries:
            for entry in entries:
                _relative_components(entry.name)
                if entry.is_symlink():
                    raise PermissionError("copy_in does not follow host reparse points")
                child_path = f"{workspace_path}/{entry.name}"
                if entry.is_dir(follow_symlinks=False):
                    child = _create_relative_directory(destination, entry.name)
                    try:
                        self._copy_host_directory(Path(entry.path), child, child_path)
                    finally:
                        _close_handles([child])
                elif entry.is_file(follow_symlinks=False):
                    with open(entry.path, "rb") as stream:
                        self._write_stream_at(destination, entry.name, stream)
                else:
                    raise PermissionError(
                        "copy_in accepts only regular files and directories"
                    )

    def read_text(self, path: str) -> str:
        import msvcrt

        parent, name, handles = self._open_parent(path)
        handle = None
        try:
            handle = _open_relative_entry(
                parent,
                name,
                desired_access=_FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                create_disposition=_FILE_OPEN,
                create_options=_FILE_NON_DIRECTORY_FILE
                | _FILE_SYNCHRONOUS_IO_NONALERT
                | _FILE_OPEN_FOR_BACKUP_INTENT,
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            )
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handle = None
            with os.fdopen(fd, "r", encoding="utf-8", newline="") as stream:
                return stream.read()
        finally:
            if handle is not None:
                _close_handles([handle])
            _close_handles(handles)

    def read_bytes_limited(self, path: str, limit: int) -> tuple[bytes, int]:
        import msvcrt

        if limit < 0:
            raise ValueError("byte limit must not be negative")
        parent, name, handles = self._open_parent(path)
        handle = None
        try:
            handle = _open_relative_entry(
                parent,
                name,
                desired_access=_FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
                create_disposition=_FILE_OPEN,
                create_options=_FILE_NON_DIRECTORY_FILE
                | _FILE_SYNCHRONOUS_IO_NONALERT
                | _FILE_OPEN_FOR_BACKUP_INTENT,
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            )
            original_bytes = max(0, int(_file_standard_info(handle).EndOfFile))
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            handle = None
            with os.fdopen(fd, "rb") as stream:
                return stream.read(limit), original_bytes
        finally:
            if handle is not None:
                _close_handles([handle])
            _close_handles(handles)

    def write_text(self, path: str, content: str) -> None:
        import io

        self._write_stream(path, io.BytesIO(content.encode("utf-8")))

    def _open_parent(
        self,
        path: str,
        *,
        create: bool = False,
        access_extra: int = 0,
    ) -> tuple[int, str, list[int]]:
        components = _relative_components(path)
        handles: list[int] = []
        parent = self._root_handle()
        try:
            for component in components[:-1]:
                parent = _open_relative_directory(
                    parent,
                    component,
                    create=create,
                    access_extra=access_extra,
                    share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                )
                handles.append(parent)
            return parent, components[-1], handles
        except BaseException:
            _close_handles(handles)
            raise

    def _write_stream(self, path: str, source) -> None:
        parent, name, handles = self._open_parent(
            path,
            create=True,
            access_extra=_FILE_ADD_SUBDIRECTORY | _FILE_ADD_FILE,
        )
        try:
            self._write_stream_at(parent, name, source)
        finally:
            _close_handles(handles)

    def _write_stream_at(self, parent: int, name: str, source) -> None:
        import msvcrt

        temporary = f".agent-{uuid4().hex}.tmp"
        handle = None
        fd = None
        try:
            handle = _open_relative_entry(
                parent,
                temporary,
                desired_access=_FILE_WRITE_DATA
                | _FILE_READ_ATTRIBUTES
                | _DELETE
                | _SYNCHRONIZE,
                create_disposition=_FILE_CREATE,
                create_options=_FILE_NON_DIRECTORY_FILE
                | _FILE_SYNCHRONOUS_IO_NONALERT
                | _FILE_OPEN_FOR_BACKUP_INTENT,
                share_access=0,
            )
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
            handle = None
            with os.fdopen(fd, "wb") as stream:
                fd = None
                try:
                    while chunk := source.read(1024 * 1024):
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                    file_handle = msvcrt.get_osfhandle(stream.fileno())
                    _rename_file_handle(file_handle, parent, name)
                except BaseException as error:
                    try:
                        _delete_handle(msvcrt.get_osfhandle(stream.fileno()))
                    except OSError as cleanup_error:
                        error.add_note(
                            f"temporary file cleanup failed: {cleanup_error}"
                        )
                    raise
        finally:
            if fd is not None:
                os.close(fd)
            elif handle is not None:
                _close_handles([handle])

    def _spawn_process(self, command: str, **kwargs) -> subprocess.Popen:
        self.ensure_started()
        environment = self._container_environment(kwargs.get("env"))
        config = self._mxc_config(
            subprocess.list2cmdline(
                [
                    environment.get(
                        "COMSPEC",
                        ntpath.join(
                            environment.get("SYSTEMROOT", r"C:\Windows"),
                            "System32",
                            "cmd.exe",
                        ),
                    ).strip('"'),
                    "/d",
                    "/c",
                    command,
                ]
            ),
            environment,
            kwargs.get("cwd", self._workspace_root),
        )
        executable = self._mxc_executable or os.environ.get("IA_MXC_EXECUTABLE")
        if not executable:
            raise RuntimeError("MXC wxc-exec.exe was not configured")
        encoded = base64.b64encode(
            json.dumps(config, separators=(",", ":")).encode("utf-8"),
        ).decode("ascii")
        kwargs["env"] = environment
        return subprocess.Popen(
            [executable, "--config-base64", encoded],
            **kwargs,
        )

    def _container_environment(self, env: dict[str, str] | None) -> dict[str, str]:
        environment = dict(os.environ) if env is None else dict(env)
        temp = ntpath.join(os.fspath(self._workspace_root), ".tmp")
        environment["TEMP"] = temp
        environment["TMP"] = temp
        python_dir = ntpath.dirname(sys.executable)
        path_entries = environment.get("PATH", "").split(os.pathsep)
        if not any(
            ntpath.normcase(ntpath.normpath(item)) == ntpath.normcase(python_dir)
            for item in path_entries
        ):
            environment["PATH"] = os.pathsep.join([python_dir, *path_entries])
        return environment

    def _mxc_config(
        self,
        command_line: str,
        environment: dict[str, str],
        cwd: str | os.PathLike[str],
    ) -> dict[str, object]:
        system_root = environment.get("SYSTEMROOT", r"C:\Windows")
        return {
            "version": "1.0.0",
            "containment": "processcontainer",
            "process": {
                "commandLine": command_line,
                "cwd": os.fspath(cwd),
                "env": [f"{key}={value}" for key, value in environment.items()],
                "inheritDefaultEnv": False,
                "timeout": 0,
            },
            "filesystem": {
                "readwritePaths": [os.fspath(self._workspace_root)],
                "readonlyPaths": self._readonly_paths(environment),
                # The policy grants read-only SystemRoot; exclude its shared
                # temp tree and require MXC's native deny enforcement. With
                # DACL fallback disabled, unsupported hosts fail closed.
                "deniedPaths": [
                    ntpath.join(system_root, "Temp"),
                ],
            },
            "fallback": {"allowDaclMutation": False},
            "ui": {"disable": False, "clipboard": "none", "injection": False},
        }

    def _readonly_paths(self, environment: dict[str, str]) -> list[str]:
        system_root = environment.get("SYSTEMROOT", r"C:\Windows")
        comspec = environment.get(
            "COMSPEC", ntpath.join(system_root, "System32", "cmd.exe")
        )
        candidates = [
            system_root,
            ntpath.dirname(comspec.strip('"')),
            ntpath.dirname(sys.executable),
            sys.prefix,
            sys.base_prefix,
            *environment.get("PATH", "").split(os.pathsep),
        ]
        workspace = ntpath.normcase(ntpath.abspath(os.fspath(self._workspace_root)))
        paths: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            path = ntpath.normpath(ntpath.abspath(candidate.strip().strip('"')))
            key = ntpath.normcase(path)
            if (
                not candidate.strip()
                or not ntpath.isabs(path)
                or key == workspace
                or key.startswith(workspace + "\\")
                or key in seen
                or not os.path.isdir(path)
            ):
                continue
            seen.add(key)
            paths.append(path)
        return paths

    def _ensure_psec_available(self) -> None:
        environment = self._container_environment(self._env)
        comspec = environment.get("COMSPEC") or ntpath.join(
            environment.get("SYSTEMROOT", r"C:\Windows"),
            "System32",
            "cmd.exe",
        )
        config = self._mxc_config(
            subprocess.list2cmdline([comspec.strip('"'), "/d", "/c", "exit 0"]),
            environment,
            self._workspace_root,
        )
        caps = probe_windows_psec_capabilities(
            executable=self._mxc_executable,
            config=config,
        )
        require_available(
            caps,
            remediation="run on Windows with MXC BaseContainer/PSEC support",
        )

    @staticmethod
    def _create_windows_job() -> None:
        # MXC owns the sandbox workload's job and tears it down when this client exits.
        return None

    @staticmethod
    def _kill_process_tree(process) -> None:
        # Terminating wxc-exec also tears down the in-flight PSEC workload.
        process.kill()

    def _root_handle(self) -> int:
        self.ensure_started()
        return self._pinned_handles[-1]


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _ntdll():
    return ctypes.WinDLL("ntdll", use_last_error=True)


def _close_handles(handles: list[int]) -> None:
    kernel32 = _kernel32()
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    for handle in reversed(handles):
        if handle:
            kernel32.CloseHandle(wintypes.HANDLE(handle))


def _open_volume_root(drive: str) -> int:
    kernel32 = _kernel32()
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    path = "\\\\?\\" + drive + "\\"
    handle = kernel32.CreateFileW(
        path,
        _FILE_READ_ATTRIBUTES | _FILE_LIST_DIRECTORY | _FILE_TRAVERSE | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _open_relative_directory(
    parent: int,
    name: str,
    *,
    create: bool,
    workspace_root: bool = False,
    access_extra: int = 0,
    share_access: int = (_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE),
) -> int:
    desired_access = _FILE_READ_ATTRIBUTES | _FILE_LIST_DIRECTORY | _FILE_TRAVERSE
    if workspace_root:
        desired_access |= (
            _FILE_WRITE_DATA | _FILE_ADD_SUBDIRECTORY | _FILE_WRITE_ATTRIBUTES
        )
    return _open_relative_entry(
        parent,
        name,
        desired_access=desired_access | access_extra | _SYNCHRONIZE,
        create_disposition=_FILE_OPEN_IF if create else _FILE_OPEN,
        create_options=_FILE_DIRECTORY_FILE
        | _FILE_SYNCHRONOUS_IO_NONALERT
        | _FILE_OPEN_FOR_BACKUP_INTENT,
        share_access=share_access,
    )


def _create_relative_directory(parent: int, name: str) -> int:
    return _open_relative_entry(
        parent,
        name,
        desired_access=(
            _FILE_READ_ATTRIBUTES
            | _FILE_LIST_DIRECTORY
            | _FILE_TRAVERSE
            | _FILE_ADD_FILE
            | _FILE_ADD_SUBDIRECTORY
            | _DELETE
            | _SYNCHRONIZE
        ),
        create_disposition=_FILE_CREATE,
        create_options=_FILE_DIRECTORY_FILE
        | _FILE_SYNCHRONOUS_IO_NONALERT
        | _FILE_OPEN_FOR_BACKUP_INTENT,
        share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
    )


def _open_relative_entry(
    parent: int,
    name: str,
    *,
    desired_access: int,
    create_disposition: int,
    create_options: int,
    share_access: int = (_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE),
    allow_reparse_leaf: bool = False,
) -> int:
    ntdll = _ntdll()
    ntdll.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    ]
    ntdll.NtCreateFile.restype = ctypes.c_long
    name_buffer = ctypes.create_unicode_buffer(name)
    unicode_name = _UnicodeString(
        len(name.encode("utf-16-le")),
        len(name.encode("utf-16-le")),
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attrs = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        wintypes.HANDLE(parent),
        ctypes.pointer(unicode_name),
        _OBJ_CASE_INSENSITIVE | (0 if allow_reparse_leaf else _OBJ_DONT_REPARSE),
        None,
        None,
    )
    io_status = _IoStatusBlock()
    result = wintypes.HANDLE()
    status = int(
        ntdll.NtCreateFile(
            ctypes.byref(result),
            desired_access,
            ctypes.byref(attrs),
            ctypes.byref(io_status),
            None,
            0x00000010,
            share_access,
            create_disposition,
            create_options | (_FILE_OPEN_REPARSE_POINT if allow_reparse_leaf else 0),
            None,
            0,
        )
    )
    if status < 0:
        _raise_ntstatus(status, name)
    return int(result.value)


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", wintypes.DWORD),
        ("DeletePending", wintypes.BOOLEAN),
        ("Directory", wintypes.BOOLEAN),
    ]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("ReparseTag", wintypes.DWORD),
    ]


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", wintypes.DWORD),
    ]


class _FileNamesInfo(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", wintypes.DWORD),
        ("FileIndex", wintypes.DWORD),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    ]


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOLEAN)]


class _FileRenameInfo(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", wintypes.BOOLEAN),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    ]


def _file_standard_info(handle: int) -> _FileStandardInfo:
    kernel32 = _kernel32()
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    info = _FileStandardInfo()
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        1,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return info


def _file_attributes(handle: int) -> int:
    kernel32 = _kernel32()
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    info = _FileAttributeTagInfo()
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        9,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(info.FileAttributes)


def _directory_entry_names(handle: int) -> list[str]:
    ntdll = _ntdll()
    ntdll.NtQueryDirectoryFile.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.BOOLEAN,
        wintypes.LPVOID,
        wintypes.BOOLEAN,
    ]
    ntdll.NtQueryDirectoryFile.restype = ctypes.c_long
    buffer = ctypes.create_string_buffer(64 * 1024)
    io_status = _IoStatusBlock()
    names: list[str] = []
    restart = True
    while True:
        status = int(
            ntdll.NtQueryDirectoryFile(
                handle,
                None,
                None,
                None,
                ctypes.byref(io_status),
                buffer,
                ctypes.sizeof(buffer),
                12,
                False,
                None,
                restart,
            )
        )
        restart = False
        if status & 0xFFFFFFFF == _STATUS_NO_MORE_FILES:
            return names
        if status < 0:
            _raise_ntstatus(status, "directory enumeration")
        offset = 0
        returned = 0
        while offset + _FileNamesInfo.FileName.offset <= len(buffer):
            next_offset, _index, name_length = struct.unpack_from(
                "<III",
                buffer,
                offset,
            )
            if name_length % 2:
                raise OSError("Windows returned an invalid directory entry")
            start = offset + _FileNamesInfo.FileName.offset
            end = start + name_length
            if end > len(buffer):
                raise OSError("Windows returned a truncated directory entry")
            name = bytes(buffer[start:end]).decode("utf-16-le", errors="surrogatepass")
            if name not in {".", ".."}:
                names.append(name)
                returned += 1
            if not next_offset:
                break
            if (
                next_offset < _FileNamesInfo.FileName.offset
                or offset + next_offset > len(buffer)
            ):
                raise OSError("Windows returned an invalid directory entry offset")
            offset += next_offset
        if not returned:
            return names


def _delete_handle(handle: int, *, clear_readonly: bool = False) -> None:
    kernel32 = _kernel32()
    if clear_readonly:
        kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        basic = _FileBasicInfo()
        if not kernel32.GetFileInformationByHandleEx(
            handle,
            0,
            ctypes.byref(basic),
            ctypes.sizeof(basic),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if basic.FileAttributes & 0x00000001:
            basic.FileAttributes &= ~0x00000001
            kernel32.SetFileInformationByHandle.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
            if not kernel32.SetFileInformationByHandle(
                handle,
                0,
                ctypes.byref(basic),
                ctypes.sizeof(basic),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
    kernel32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    info = _FileDispositionInfo(True)
    if not kernel32.SetFileInformationByHandle(
        handle,
        4,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _delete_directory_contents(directory: int) -> None:
    for name in _directory_entry_names(directory):
        try:
            entry = _open_relative_entry(
                directory,
                name,
                desired_access=(
                    _DELETE
                    | _FILE_READ_ATTRIBUTES
                    | _FILE_LIST_DIRECTORY
                    | _FILE_WRITE_ATTRIBUTES
                    | _SYNCHRONIZE
                ),
                create_disposition=_FILE_OPEN,
                create_options=_FILE_SYNCHRONOUS_IO_NONALERT
                | _FILE_OPEN_FOR_BACKUP_INTENT,
                share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                allow_reparse_leaf=True,
            )
        except FileNotFoundError:
            continue
        try:
            attrs = _file_attributes(entry)
            if not attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
                info = _file_standard_info(entry)
                if info.Directory:
                    _delete_directory_contents(entry)
            _delete_handle(entry, clear_readonly=True)
        finally:
            _close_handles([entry])


def _rename_file_handle(handle: int, parent: int, name: str) -> None:
    ntdll = _ntdll()
    ntdll.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    ntdll.NtSetInformationFile.restype = ctypes.c_long
    encoded_name = name.encode("utf-16-le")
    name_offset = _FileRenameInfo.FileName.offset
    buffer = ctypes.create_string_buffer(name_offset + len(encoded_name))
    info = ctypes.cast(buffer, ctypes.POINTER(_FileRenameInfo)).contents
    info.ReplaceIfExists = 1
    info.RootDirectory = wintypes.HANDLE(parent)
    info.FileNameLength = len(encoded_name)
    ctypes.memmove(
        ctypes.addressof(buffer) + name_offset, encoded_name, len(encoded_name)
    )
    io_status = _IoStatusBlock()
    status = int(
        ntdll.NtSetInformationFile(
            handle,
            ctypes.byref(io_status),
            buffer,
            ctypes.sizeof(buffer),
            10,
        )
    )
    if status < 0:
        _raise_ntstatus(status, name)


def _open_directory_chain(root: Path, *, create: bool) -> list[int]:
    path = ntpath.normpath(os.fspath(root))
    drive, tail = ntpath.splitdrive(path)
    if not drive or not tail.startswith("\\") or path.startswith("\\\\"):
        raise ValueError("windows_psec workspace must be a local drive path")
    components = [item for item in tail.split("\\") if item]
    if not components:
        raise ValueError("windows_psec cannot use a volume root as workspace")
    handles = [_open_volume_root(drive)]
    try:
        for index, item in enumerate(components):
            if item in {".", ".."} or ":" in item:
                raise ValueError("invalid Windows workspace path component")
            final = index == len(components) - 1
            try:
                child = _open_relative_directory(
                    handles[-1],
                    item,
                    create=False,
                    workspace_root=final,
                )
            except FileNotFoundError:
                if not create:
                    raise
                parent_index = len(handles) - 1
                if parent_index == 0:
                    raise PermissionError(
                        "cannot create workspace directly under a volume root"
                    )
                parent = _open_relative_directory(
                    handles[-2],
                    components[index - 1],
                    create=False,
                    access_extra=0x00000004 | 0x00000002,
                )
                _close_handles([handles[-1]])
                handles[-1] = parent
                child = _open_relative_directory(
                    handles[-1],
                    item,
                    create=True,
                    workspace_root=final,
                    access_extra=0 if final else 0x00000004 | 0x00000002,
                )
            handles.append(child)
        return handles
    except BaseException:
        _close_handles(handles)
        raise


def _raise_ntstatus(status: int, name: str) -> None:
    if status & 0xFFFFFFFF == _STATUS_REPARSE_POINT_ENCOUNTERED:
        raise PermissionError(f"reparse point rejected in workspace path: {name}")
    ntdll = _ntdll()
    ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
    error = int(ntdll.RtlNtStatusToDosError(status))
    raise ctypes.WinError(error)


class _FileIdInfo(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", ctypes.c_ubyte * 16),
    ]


def _file_identity(handle: int) -> dict[str, str]:
    kernel32 = _kernel32()
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    info = _FileIdInfo()
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        18,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "volume_serial": f"{info.VolumeSerialNumber:016x}",
        "file_id": bytes(info.FileId).hex(),
    }


def _glob_match(rel_path: str, pattern: str) -> bool:
    if pattern in ("", "*"):
        return True
    if "**" in pattern:
        normalized = pattern.replace("**/", "").replace("**", "*")
        return fnmatch.fnmatch(rel_path, normalized) or fnmatch.fnmatch(
            ntpath.basename(rel_path),
            normalized,
        )
    return fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(
        ntpath.basename(rel_path),
        pattern,
    )


def _relative_components(
    path: str,
    *,
    allow_empty: bool = False,
    allow_glob: bool = False,
) -> list[str]:
    if not isinstance(path, str) or "\x00" in path:
        raise PermissionError("invalid workspace path")
    normalized = path.replace("/", "\\")
    if normalized.startswith("\\") or ":" in normalized:
        raise PermissionError("workspace paths must be relative")
    parts = normalized.split("\\")
    if allow_empty and (not path or path == "."):
        return []
    if any(part in {"", ".", ".."} for part in parts):
        raise PermissionError("workspace path traversal rejected")
    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{digit}" for digit in "123456789¹²³"),
        *(f"LPT{digit}" for digit in "123456789¹²³"),
    }
    for part in parts:
        if part.endswith((" ", ".")):
            raise PermissionError("invalid Windows path component")
        if any(ord(char) < 32 or char in '<>"|' for char in part):
            raise PermissionError("invalid Windows path component")
        if not allow_glob and any(char in "*?" for char in part):
            raise PermissionError("wildcards are not valid in workspace paths")
        stem = part.split(".", 1)[0].rstrip(" .").upper()
        if allow_glob and any(char in stem for char in "*?[]"):
            continue
        if stem in reserved_names:
            raise PermissionError("Windows device path rejected")
        try:
            part.encode("utf-16-le")
        except UnicodeEncodeError as error:
            raise PermissionError("invalid Windows path component") from error
    return parts
