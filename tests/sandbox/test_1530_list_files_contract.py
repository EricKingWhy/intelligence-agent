from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from agent_harness.sandbox.capabilities import probe_docker_capabilities
from agent_harness.sandbox.docker import DockerSandbox
from agent_harness.sandbox.local import LocalSubprocessSandbox


@pytest.fixture(params=['local', 'docker', 'psec'])
def sandbox(request: pytest.FixtureRequest, tmp_path: Path):
    backend = request.param
    if backend == 'local':
        sandbox = LocalSubprocessSandbox(tmp_path / 'workspace')
    elif backend == 'docker':
        caps = probe_docker_capabilities()
        if not caps.available:
            pytest.skip(caps.reason)
        name = 'issue1530-' + uuid4().hex
        sandbox = DockerSandbox(container_name=name, volume_name=name)
    else:
        if os.name != 'nt':
            pytest.skip('PSEC requires Windows')
        from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

        configured = os.environ.get('IA_MXC_EXECUTABLE')
        bundled = Path(__file__).resolve().parents[2] / 'desktop/node_modules/@microsoft/mxc-sdk/bin/x64/wxc-exec.exe'
        candidates = [Path(configured)] if configured else []
        executable = next((path for path in [*candidates, bundled] if path.is_file()), None)
        if executable is None:
            pytest.skip('PSEC requires the installed MXC SDK')
        sandbox = WindowsPsecSandbox(tmp_path / 'workspace', mxc_executable=executable)
    try:
        for path in ('root.py', 'pkg/inner.py', 'pkg/deep/up.PY', '.hidden.py', 'pkg/notes.txt'):
            sandbox.write_text(path, 'x')
        yield backend, sandbox
    finally:
        if backend == 'docker':
            sandbox.delete()
        else:
            sandbox.stop()


def test_list_files_preserves_patterns_and_platform_validation(sandbox):
    backend, sandbox = sandbox
    all_files = ['.hidden.py', 'pkg/deep/up.PY', 'pkg/inner.py', 'pkg/notes.txt', 'root.py']
    lower = ['.hidden.py', 'pkg/inner.py', 'root.py']
    if os.name == 'nt':
        lower.insert(1, 'pkg/deep/up.PY')
    assert sandbox.list_files('') == all_files
    assert sandbox.list_files('*') == all_files
    assert sandbox.list_files('*.py') == lower
    assert sandbox.list_files('**/*.py') == lower
    pkg_files = ['pkg/deep/up.PY', 'pkg/inner.py'] if os.name == 'nt' else ['pkg/inner.py']
    assert sandbox.list_files('pkg/*.py') == pkg_files
    assert sandbox.list_files(r'pkg\*.py') == (pkg_files if os.name == 'nt' else [])
    assert sandbox.list_files('*.PY') == (lower if os.name == 'nt' else ['pkg/deep/up.PY'])
    for pattern in ('C:/outside/*.py', '../*.py'):
        if backend == 'psec':
            with pytest.raises(PermissionError):
                sandbox.list_files(pattern)
        else:
            assert sandbox.list_files(pattern) == []
