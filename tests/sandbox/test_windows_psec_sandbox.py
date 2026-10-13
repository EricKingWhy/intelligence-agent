from __future__ import annotations

import json
import ntpath
import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from agent_harness.sandbox.registry import WorkspaceRegistry


@pytest.fixture(autouse=True)
def configure_mxc_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "nt":
        return
    configured = os.environ.get("IA_MXC_EXECUTABLE")
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[2]
        / "desktop"
        / "node_modules"
        / "@microsoft"
        / "mxc-sdk"
        / "bin"
        / "x64"
        / "wxc-exec.exe",
    ]
    executable = next((path for path in candidates if path and path.is_file()), None)
    if executable is None:
        pytest.skip("install @microsoft/mxc-sdk or set IA_MXC_EXECUTABLE")
    if not (executable.parent / "mxc_ffi.dll").is_file():
        pytest.fail(f"MXC runtime DLL is missing beside {executable}")
    monkeypatch.setenv("IA_MXC_EXECUTABLE", str(executable))


def _workspace_dacl(path: Path) -> str:
    descriptor_path = None
    try:
        descriptor_fd, descriptor_name = tempfile.mkstemp(prefix="ia729-acl-")
        os.close(descriptor_fd)
        descriptor_path = Path(descriptor_name)
        descriptor_path.unlink()
        result = subprocess.run(
            ["icacls.exe", str(path), "/save", str(descriptor_path), "/c"],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"icacls /save failed: {result.stderr!r}")
        lines = descriptor_path.read_bytes().decode("utf-16-le").splitlines()
        if len(lines) < 2:
            raise RuntimeError(f"icacls returned an incomplete DACL: {lines!r}")
        return "\n".join(lines[1:])
    finally:
        if descriptor_path is not None:
            descriptor_path.unlink(missing_ok=True)


def _workspace_integrity_label(path: Path) -> bytes:
    import ctypes
    from ctypes import wintypes

    get_file_security = ctypes.WinDLL("advapi32", use_last_error=True).GetFileSecurityW
    get_file_security.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_file_security.restype = wintypes.BOOL
    label_security_information = 0x00000010
    required = wintypes.DWORD()
    get_file_security(
        str(path),
        label_security_information,
        None,
        0,
        ctypes.byref(required),
    )
    if required.value == 0:
        raise ctypes.WinError(ctypes.get_last_error())

    descriptor = ctypes.create_string_buffer(required.value)
    if not get_file_security(
        str(path),
        label_security_information,
        descriptor,
        required.value,
        ctypes.byref(required),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return descriptor.raw[: required.value]


def _grant_everyone_full_control(path: Path) -> None:
    result = subprocess.run(
        ["icacls.exe", str(path), "/grant:r", "*S-1-1-0:(OI)(CI)F"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"icacls /grant failed: {result.stderr!r}")


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_startup_probe_requires_mxc_native_deny_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_harness.sandbox.windows_psec as psec_module
    from agent_harness.sandbox.capabilities import SandboxCapabilities
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    observed: dict[str, object] = {}

    def probe(*, executable: str | None, config: dict[str, object]):
        observed.update(config)
        return SandboxCapabilities(backend="windows_psec", available=True)

    monkeypatch.setattr(psec_module, "probe_windows_psec_capabilities", probe)
    sandbox = WindowsPsecSandbox(tmp_path / "workspace")

    try:
        sandbox.ensure_started()

        filesystem = observed["filesystem"]
        assert isinstance(filesystem, dict)
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        assert filesystem["deniedPaths"] == [
            ntpath.join(system_root, "Temp"),
        ]
        assert observed["fallback"] == {"allowDaclMutation": False}
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_registry_selects_windows_psec_and_persists_workspace_identity(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    workspace_root = tmp_path / "workspace"
    registry = WorkspaceRegistry(root=registry_root, backend="local")

    registry.create(
        "windows-session",
        workspace_root=workspace_root,
        backend="windows_psec",
    )

    mapping = json.loads(
        (registry_root / "workspaces" / "windows-session.json").read_text(
            encoding="utf-8",
        )
    )
    assert mapping["backend"] == "windows_psec"
    identity = mapping["workspace_identity"]
    assert set(identity) == {"volume_serial", "file_id"}
    assert all(isinstance(value, str) and value for value in identity.values())


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_registry_recovery_rejects_replaced_workspace_root(tmp_path: Path) -> None:
    from agent_harness.sandbox.windows_psec import WorkspaceIdentityError

    workspace = tmp_path / "workspace"
    registry = WorkspaceRegistry(root=tmp_path / "registry", backend="local")
    registry.create(
        "replaced-session",
        workspace_root=workspace,
        backend="windows_psec",
    )
    registry.stop("replaced-session")
    workspace.rename(tmp_path / "original-workspace")
    workspace.mkdir()
    canary = workspace / "replacement.txt"
    canary.write_text("replacement root", encoding="utf-8")
    before_dacl = _workspace_dacl(workspace)

    with pytest.raises(WorkspaceIdentityError):
        registry.get("replaced-session")

    assert canary.read_text(encoding="utf-8") == "replacement root"
    assert _workspace_dacl(workspace) == before_dacl


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_starting_again_preserves_workspace_dacl_and_integrity_label(
    tmp_path: Path,
) -> None:
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    sandbox = WindowsPsecSandbox(tmp_path / "workspace")
    sandbox.write_text("seed.txt", "seed")
    before_dacl = _workspace_dacl(sandbox.workspace_root)
    before_label = _workspace_integrity_label(sandbox.workspace_root)

    try:
        sandbox.ensure_started()
        assert _workspace_dacl(sandbox.workspace_root) == before_dacl
        assert _workspace_integrity_label(sandbox.workspace_root) == before_label
        sandbox.stop()
        sandbox.ensure_started()
        assert _workspace_dacl(sandbox.workspace_root) == before_dacl
        assert _workspace_integrity_label(sandbox.workspace_root) == before_label
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_exec_allows_workspace_write_and_denies_sibling_write(tmp_path: Path) -> None:
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    inside = workspace / "inside.txt"
    sandbox = WindowsPsecSandbox(workspace)

    result = sandbox.exec(
        "echo inside>inside.txt & echo outside>..\\outside.txt",
        timeout=15,
    )

    assert inside.read_text(encoding="utf-8").strip() == "inside"
    assert not outside.exists()
    assert result.exit_code != 0
    sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_exec_runs_python_under_workspace_only_write_restriction(
    tmp_path: Path,
) -> None:
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "python_probe.py"
    script.write_text(
        "import subprocess\n"
        "child = subprocess.run(\n"
        "    ['python.exe', '-c', \"print('python-child-ok')\"],\n"
        "    check=True, capture_output=True, text=True,\n"
        ")\n"
        "assert child.stdout.strip() == 'python-child-ok'\n"
        "print('python-ok')\n",
        encoding="utf-8",
    )
    sandbox = WindowsPsecSandbox(workspace)
    try:
        result = sandbox.exec(
            f"python {script.name}",
            timeout=15,
        )
        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip() == "python-ok"
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_exec_can_initialize_windows_cng_under_workspace_only_write_restriction(
    tmp_path: Path,
) -> None:
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "cng_probe.py"
    script.write_text(
        "import ctypes\n"
        "bcrypt = ctypes.WinDLL('bcrypt')\n"
        "provider = ctypes.c_void_p()\n"
        "open_provider = bcrypt.BCryptOpenAlgorithmProvider\n"
        "open_provider.argtypes = [ctypes.POINTER(ctypes.c_void_p), "
        "ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]\n"
        "open_provider.restype = ctypes.c_long\n"
        "status = open_provider(ctypes.byref(provider), 'SHA256', None, 0)\n"
        "if status != 0: raise SystemExit(status)\n"
        "close_provider = bcrypt.BCryptCloseAlgorithmProvider\n"
        "close_provider.argtypes = [ctypes.c_void_p, ctypes.c_ulong]\n"
        "close_provider.restype = ctypes.c_long\n"
        "if close_provider(provider, 0) != 0: raise SystemExit(1)\n"
        "print('cng-ok')\n",
        encoding="utf-8",
    )
    sandbox = WindowsPsecSandbox(workspace)
    try:
        result = sandbox.exec(
            f"python {script.name}",
            timeout=15,
        )
        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip() == "cng-ok"
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_psec_enforces_resolved_targets_during_bidirectional_junction_swaps(
    tmp_path: Path,
) -> None:
    import threading
    import time

    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_canary = outside / "canary.txt"
    outside_canary.write_text("external root", encoding="utf-8")
    workspace_canary = workspace / "guard.txt"
    workspace_canary.parent.mkdir()
    workspace_canary.write_text("workspace target", encoding="utf-8")
    outside_target = outside / "inbound-real"
    outside_target.mkdir()
    external_guard = outside_target / "guard.txt"
    external_guard.write_text("external target", encoding="utf-8")
    sandbox = WindowsPsecSandbox(workspace)
    sandbox.write_text("linked/guard.txt", "internal directory")

    def create_junction(link: Path, target: Path) -> None:
        result = subprocess.run(
            f'mklink /J "{link}" "{target}"',
            capture_output=True,
            text=True,
            check=False,
            shell=True,
        )
        if result.returncode != 0:
            pytest.skip(
                f"Windows junction creation unavailable: {result.stdout}{result.stderr}"
            )

    def run_during_swap(
        command: str,
        first: Path,
        second: Path,
        temporary: Path,
        *,
        first_is_junction: bool,
    ) -> int:
        stop = threading.Event()
        ready = threading.Event()
        swaps = 0

        def replace_entries() -> None:
            nonlocal swaps
            ready.set()
            while not stop.is_set():
                try:
                    if not os.path.lexists(temporary):
                        if not os.path.lexists(first):
                            time.sleep(0.001)
                            continue
                        os.rename(first, temporary)
                    if os.path.lexists(second) and not os.path.lexists(first):
                        os.rename(second, first)
                    if os.path.lexists(temporary) and not os.path.lexists(second):
                        os.rename(temporary, second)
                        swaps += 1
                        time.sleep(0.001)
                except OSError:
                    time.sleep(0.001)

        worker_result = []
        mutator = threading.Thread(target=replace_entries, daemon=True)
        mutator.start()
        assert ready.wait(timeout=2)
        try:
            worker_result.append(sandbox.exec(command, timeout=30))
        finally:
            stop.set()
            mutator.join(timeout=5)
        assert not mutator.is_alive()
        assert worker_result and not worker_result[0].timed_out
        if os.path.lexists(temporary):
            if not os.path.lexists(first):
                os.rename(temporary, first)
            elif not os.path.lexists(second):
                os.rename(temporary, second)
                swaps += 1
        is_junction = getattr(first, "is_junction", None)
        first_is_link = (
            is_junction()
            if callable(is_junction)
            else bool(
                first.lstat().st_file_attributes
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
        )
        if first_is_link != first_is_junction:
            os.rename(first, temporary)
            os.rename(second, first)
            os.rename(temporary, second)
        return swaps

    workspace_link = workspace / "linked"
    workspace_reparse = workspace / "reparse"
    workspace_swap = workspace / "workspace-swap"
    create_junction(workspace_reparse, outside)
    external_inbound = outside / "inbound"
    external_swap = outside / "inbound-swap"
    create_junction(external_inbound, workspace)

    before_workspace_dacl = _workspace_dacl(workspace)
    before_outside_dacl = _workspace_dacl(outside)
    before_external_target_dacl = _workspace_dacl(outside_target)
    try:
        outward_swaps = run_during_swap(
            r"for /L %i in (1,1,500) do @echo child-write>linked\guard.txt",
            workspace_link,
            workspace_reparse,
            workspace_swap,
            first_is_junction=False,
        )
        inward_swaps = run_during_swap(
            r"for /L %i in (1,1,500) do @echo child-write>..\outside\inbound\guard.txt",
            external_inbound,
            outside_target,
            external_swap,
            first_is_junction=True,
        )
        assert outward_swaps > 0
        assert inward_swaps > 0
        assert workspace_canary.read_text(encoding="utf-8").strip() == "child-write"
        assert outside_canary.read_text(encoding="utf-8") == "external root"
        assert external_guard.read_text(encoding="utf-8") == "external target"
        assert _workspace_dacl(workspace) == before_workspace_dacl
        assert _workspace_dacl(outside) == before_outside_dacl
        assert _workspace_dacl(outside_target) == before_external_target_dacl
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_psec_timeout_terminates_nested_process_tree(tmp_path: Path) -> None:
    import time

    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "timeout_probe.py"
    marker = workspace / "child-survived.txt"
    script.write_text(
        "import subprocess, time\n"
        "subprocess.Popen([\n"
        "    'python.exe', '-c',\n"
        "    \"import time; time.sleep(2); open('child-survived.txt', 'w').write('alive')\",\n"
        "])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    sandbox = WindowsPsecSandbox(workspace)
    try:
        result = sandbox.exec("python timeout_probe.py", timeout=1)
        assert result.timed_out
        time.sleep(2.5)
        assert not marker.exists(), "MXC wrapper exit left a nested process running"
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_exec_cannot_write_to_everyone_writable_directory(tmp_path: Path) -> None:
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    outside = tmp_path / "ambient-writable"
    outside.mkdir()
    _grant_everyone_full_control(outside)
    (outside / "host-check.txt").write_text("host", encoding="utf-8")
    (outside / "host-check.txt").unlink()
    target = outside / "outside.txt"
    sandbox = WindowsPsecSandbox(workspace)
    try:
        result = sandbox.exec(
            "echo escaped>..\\ambient-writable\\outside.txt",
            timeout=15,
        )
        assert not target.exists()
        assert result.exit_code != 0
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_read_text_rejects_reparse_target_without_reading_external_file(
    tmp_path: Path,
) -> None:
    import subprocess

    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "secret.txt"
    canary.write_text("external secret", encoding="utf-8")
    sandbox = WindowsPsecSandbox(workspace)
    sandbox.ensure_started()
    link = workspace / "linked"
    try:
        result = subprocess.run(
            f'mklink /J "{link}" "{outside}"',
            capture_output=True,
            text=True,
            check=False,
            shell=True,
        )
        if result.returncode != 0:
            pytest.skip(
                f"Windows junction creation unavailable: {result.stdout}{result.stderr}"
            )

        with pytest.raises(PermissionError):
            sandbox.read_text("linked/secret.txt")

        with pytest.raises(OSError):
            sandbox.read_text("linked")

        with pytest.raises(PermissionError):
            sandbox.write_text("linked/secret.txt", "changed")

        assert canary.read_text(encoding="utf-8") == "external secret"
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_final_file_symlink_is_not_followed_and_atomic_write_replaces_the_link(
    tmp_path: Path,
) -> None:
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "secret.txt"
    canary.write_text("external secret", encoding="utf-8")
    sandbox = WindowsPsecSandbox(workspace)
    link = workspace / "secret-link.txt"
    try:
        result = subprocess.run(
            f'mklink "{link}" "{canary}"',
            capture_output=True,
            text=True,
            check=False,
            shell=True,
        )
        if result.returncode != 0:
            pytest.skip(
                f"Windows file symlink creation unavailable: {result.stdout}{result.stderr}"
            )

        with pytest.raises(PermissionError):
            sandbox.read_text("secret-link.txt")

        sandbox.write_text("secret-link.txt", "workspace content")

        assert link.read_text(encoding="utf-8") == "workspace content"
        assert canary.read_text(encoding="utf-8") == "external secret"
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_concurrent_junction_swap_cannot_write_outside_or_change_root_acl(
    tmp_path: Path,
) -> None:
    import threading

    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("external secret", encoding="utf-8")
    sandbox = WindowsPsecSandbox(workspace)
    sandbox.write_text("linked/canary.txt", "internal content")
    sandbox.ensure_started()
    result = subprocess.run(
        f'mklink /J "{workspace / "reparse"}" "{outside}"',
        capture_output=True,
        text=True,
        check=False,
        shell=True,
    )
    if result.returncode != 0:
        sandbox.stop()
        pytest.skip(
            f"Windows junction creation unavailable: {result.stdout}{result.stderr}"
        )

    before_workspace_dacl = _workspace_dacl(workspace)
    before_outside_dacl = _workspace_dacl(outside)
    stop = threading.Event()
    ready = threading.Event()
    swaps = 0
    failures: list[str] = []
    successful_reads = 0
    successful_writes = 0
    temporary = workspace / "swap-temp"
    recreated = workspace / "swap-recreated"
    linked = workspace / "linked"
    reparse = workspace / "reparse"

    def replace_entries() -> None:
        nonlocal swaps
        ready.set()
        while not stop.is_set():
            try:
                if os.path.lexists(recreated):
                    shutil.rmtree(recreated)
                if not os.path.lexists(temporary):
                    if not os.path.lexists(linked):
                        time.sleep(0.001)
                        continue
                    os.rename(linked, temporary)
                # A concurrent write may create a new directory during the
                # brief gap. Move that directory aside before installing the
                # junction, then remove it after the rotation.
                if os.path.lexists(linked):
                    os.rename(linked, recreated)
                if os.path.lexists(reparse) and not os.path.lexists(linked):
                    os.rename(reparse, linked)
                if os.path.lexists(temporary) and not os.path.lexists(reparse):
                    os.rename(temporary, reparse)
                    swaps += 1
                if os.path.lexists(recreated):
                    shutil.rmtree(recreated)
            except OSError as error:
                if len(failures) < 5:
                    failures.append(
                        f"{error.__class__.__name__}: {error}; "
                        f"linked={os.path.lexists(linked)}, "
                        f"temp={os.path.lexists(temporary)}, "
                        f"reparse={os.path.lexists(reparse)}, "
                        f"recreated={os.path.lexists(recreated)}"
                    )
                time.sleep(0.001)

    thread = threading.Thread(target=replace_entries, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    try:
        for _ in range(300):
            try:
                content = sandbox.read_text("linked/canary.txt")
                assert content in {"internal content", "race-write"}
                successful_reads += 1
            except (FileNotFoundError, PermissionError):
                pass
            try:
                sandbox.write_text("linked/canary.txt", "race-write")
                successful_writes += 1
            except (FileNotFoundError, PermissionError):
                pass
        assert canary.read_text(encoding="utf-8") == "external secret"
        assert _workspace_dacl(workspace) == before_workspace_dacl
        assert _workspace_dacl(outside) == before_outside_dacl
    finally:
        stop.set()
        thread.join(timeout=5)
        sandbox.stop()
    assert not thread.is_alive()
    assert swaps > 0, failures[:1]
    assert successful_reads > 0
    assert successful_writes > 0


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_read_write_text_is_handle_relative_and_preserves_line_endings(
    tmp_path: Path,
) -> None:
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    sandbox = WindowsPsecSandbox(tmp_path / "workspace")
    try:
        sandbox.write_text("nested/file.txt", "first\r\nsecond\n")
        assert sandbox.read_text("nested/file.txt") == "first\r\nsecond\n"

        sandbox.write_text("nested/file.txt", "replacement")
        assert sandbox.read_text("nested/file.txt") == "replacement"

        long_name = f"{'x' * 250}.txt"
        sandbox.write_text(long_name, "long name")
        assert sandbox.read_text(long_name) == "long name"
    finally:
        sandbox.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
async def test_write_tool_uses_handle_safe_kind_probe(tmp_path: Path) -> None:
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox
    from agent_harness.tools.write import WriteTool

    sandbox = WindowsPsecSandbox(tmp_path / "workspace")
    sandbox.write_text("folder/seed.txt", "seed")

    def reject_path_resolution(_path: str) -> Path:
        raise AssertionError("WriteTool must not resolve a PSEC path by name")

    sandbox.resolve_within_workspace = reject_path_resolution  # type: ignore[method-assign]
    try:
        tool = WriteTool(sandbox)
        result = await tool.execute(
            tool.args_schema(path="folder", content="not a file"),
        )

        assert not result.ok
        assert result.error_code == "INVALID_ARGUMENT"
        assert sandbox.path_kind("folder") == "directory"
    finally:
        sandbox.stop()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
async def test_read_tool_loads_project_instructions_through_windows_sandbox(
    tmp_path: Path,
) -> None:
    from agent_harness.context.project_instructions import ProjectInstructionStore
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox
    from agent_harness.tools.read import ReadTool

    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("outside instructions", encoding="utf-8")
    workspace = tmp_path / "workspace"
    sandbox = WindowsPsecSandbox(workspace)
    sandbox.write_text("AGENTS.md", "workspace instructions")
    sandbox.write_text("nested/AGENTS.md", "nested instructions")
    sandbox.write_text("nested/module.py", "value = 1")
    store = ProjectInstructionStore(max_file_bytes=4096, max_total_bytes=8192)
    store.load_for_sandbox_session("windows-instructions", sandbox)
    tool = ReadTool(
        sandbox,
        project_instructions_loader=lambda path: store.load_for_sandbox_path(
            "windows-instructions",
            sandbox,
            path,
        ),
    )

    try:
        result = await tool.execute(tool.args_schema(path="nested/module.py"))
        snapshot = store.load_for_sandbox_path(
            "windows-instructions",
            sandbox,
            "nested/module.py",
        )

        assert result.ok
        assert "workspace instructions" in (snapshot.prompt_text or "")
        assert "nested instructions" in (snapshot.prompt_text or "")
        assert "outside instructions" not in (snapshot.prompt_text or "")
        assert snapshot.source_paths == (
            workspace / "AGENTS.md",
            workspace / "nested" / "AGENTS.md",
        )
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_list_files_is_handle_relative_and_skips_reparse_points(
    tmp_path: Path,
) -> None:
    import subprocess

    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("external secret", encoding="utf-8")
    sandbox = WindowsPsecSandbox(workspace)
    try:
        sandbox.write_text("nested/visible.txt", "visible")
        sandbox.ensure_started()
        result = subprocess.run(
            f'mklink /J "{workspace / "linked"}" "{outside}"',
            capture_output=True,
            text=True,
            check=False,
            shell=True,
        )
        if result.returncode != 0:
            pytest.skip(
                f"Windows junction creation unavailable: {result.stdout}{result.stderr}"
            )

        assert sandbox.list_files("*") == ["nested/visible.txt"]
        assert sandbox.list_files("**/*.txt") == ["nested/visible.txt"]
        assert (outside / "secret.txt").read_text(encoding="utf-8") == "external secret"
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_copy_in_imports_host_files_and_directories(tmp_path: Path) -> None:
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "input.txt").write_text("copied", encoding="utf-8")
    single_file = tmp_path / "single.txt"
    single_file.write_text("single", encoding="utf-8")
    sandbox = WindowsPsecSandbox(tmp_path / "workspace")
    try:
        sandbox.copy_in(source, "imported")
        sandbox.copy_in(single_file, "one.txt")
        assert sandbox.read_text("imported/nested/input.txt") == "copied"
        assert sandbox.read_text("one.txt") == "single"
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_copy_in_rejects_host_directory_junction(tmp_path: Path) -> None:
    import subprocess

    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("external secret", encoding="utf-8")
    source = tmp_path / "source-junction"
    result = subprocess.run(
        f'mklink /J "{source}" "{outside}"',
        capture_output=True,
        text=True,
        check=False,
        shell=True,
    )
    if result.returncode != 0:
        pytest.skip(
            f"Windows junction creation unavailable: {result.stdout}{result.stderr}"
        )

    workspace = tmp_path / "workspace"
    sandbox = WindowsPsecSandbox(workspace)
    try:
        with pytest.raises(PermissionError, match="reparse"):
            sandbox.copy_in(source, "imported")
        assert not (workspace / "imported" / "secret.txt").exists()
        assert (outside / "secret.txt").read_text(encoding="utf-8") == "external secret"
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_copy_in_rejects_nested_host_directory_junction(tmp_path: Path) -> None:
    import subprocess

    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("external secret", encoding="utf-8")
    link = source / "linked"
    result = subprocess.run(
        f'mklink /J "{link}" "{outside}"',
        capture_output=True,
        text=True,
        check=False,
        shell=True,
    )
    if result.returncode != 0:
        pytest.skip(
            f"Windows junction creation unavailable: {result.stdout}{result.stderr}"
        )

    workspace = tmp_path / "workspace"
    sandbox = WindowsPsecSandbox(workspace)
    try:
        with pytest.raises(PermissionError, match="reparse"):
            sandbox.copy_in(source, "imported")
        assert not (workspace / "imported" / "linked" / "secret.txt").exists()
        assert (outside / "secret.txt").read_text(encoding="utf-8") == "external secret"
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_copy_in_rejects_host_file_under_directory_junction(tmp_path: Path) -> None:
    import subprocess

    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    outside = tmp_path / "outside"
    outside.mkdir()
    source_file = outside / "secret.txt"
    source_file.write_text("external secret", encoding="utf-8")
    source = tmp_path / "source-junction"
    result = subprocess.run(
        f'mklink /J "{source}" "{outside}"',
        capture_output=True,
        text=True,
        check=False,
        shell=True,
    )
    if result.returncode != 0:
        pytest.skip(
            f"Windows junction creation unavailable: {result.stdout}{result.stderr}"
        )

    workspace = tmp_path / "workspace"
    sandbox = WindowsPsecSandbox(workspace)
    try:
        with pytest.raises(PermissionError, match="reparse"):
            sandbox.copy_in(source / source_file.name, "imported.txt")
        assert not (workspace / "imported.txt").exists()
        assert source_file.read_text(encoding="utf-8") == "external secret"
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_copy_in_keeps_open_source_directory_during_junction_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_harness.sandbox import windows_psec

    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "inside.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("external secret", encoding="utf-8")
    workspace = tmp_path / "workspace"
    sandbox = windows_psec.WindowsPsecSandbox(workspace)
    create_directory = windows_psec._create_relative_directory
    replacement_blocked = False

    def create_and_replace(parent: int, name: str) -> int:
        nonlocal replacement_blocked
        handle = create_directory(parent, name)
        if name == "nested":
            with pytest.raises(PermissionError):
                nested.rename(source / "nested-before-swap")
            replacement_blocked = True
        return handle

    monkeypatch.setattr(windows_psec, "_create_relative_directory", create_and_replace)
    try:
        sandbox.copy_in(source, "imported")
        assert replacement_blocked
        assert not (workspace / "imported" / "nested" / "secret.txt").exists()
        assert sandbox.read_text("imported/nested/inside.txt") == "inside"
        assert (outside / "secret.txt").read_text(encoding="utf-8") == "external secret"
    finally:
        sandbox.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_delete_removes_workspace_without_following_junction(tmp_path: Path) -> None:
    import subprocess

    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("untouched", encoding="utf-8")
    sandbox = WindowsPsecSandbox(workspace)
    sandbox.write_text("internal.txt", "remove me")
    sandbox.ensure_started()
    result = subprocess.run(
        f'mklink /J "{workspace / "linked"}" "{outside}"',
        capture_output=True,
        text=True,
        check=False,
        shell=True,
    )
    if result.returncode != 0:
        sandbox.stop()
        pytest.skip(
            f"Windows junction creation unavailable: {result.stdout}{result.stderr}"
        )

    sandbox.delete()

    assert not workspace.exists()
    assert canary.read_text(encoding="utf-8") == "untouched"


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
def test_delete_removes_read_only_workspace_files(tmp_path: Path) -> None:
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    workspace = tmp_path / "workspace"
    sandbox = WindowsPsecSandbox(workspace)
    sandbox.write_text("nested/readonly.txt", "remove me")
    os.chmod(workspace / "nested" / "readonly.txt", stat.S_IREAD)

    sandbox.delete()

    assert not workspace.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PSEC sandbox is Windows-only")
@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "C:/outside.txt", "folder/CON.txt", "folder/name.", "a:b"],
)
def test_workspace_path_rejects_windows_escape_and_device_names(
    tmp_path: Path,
    path: str,
) -> None:
    from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

    sandbox = WindowsPsecSandbox(tmp_path / "workspace")
    try:
        with pytest.raises(PermissionError):
            sandbox.read_text(path)
    finally:
        sandbox.stop()
