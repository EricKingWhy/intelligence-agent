"""#361 [W-16]: python-runtime.lock.json validation.

The Windows installer bundles an offline Python runtime; this lockfile is the
single pin for the interpreter build and the full win_amd64 wheel closure.
These tests keep the lock well-formed (schema, hashes, name/version
consistency) and in sync with pyproject's runtime dependencies.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = REPO_ROOT / "desktop" / "installer" / "python-runtime.lock.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@pytest.fixture(scope="module")
def lock():
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def test_lockfile_exists_and_parses(lock):
    assert LOCK_PATH.is_file()
    assert lock["schemaVersion"] == 1


def test_target_is_windows_x64(lock):
    assert lock["target"] == {"platform": "win32", "arch": "x64"}


def test_python_pin(lock):
    python = lock["python"]
    assert python["implementation"] == "cpython"
    major, minor = (int(x) for x in str(python["version"]).split(".")[:2])
    # pyproject requires-python >= 3.11
    assert (major, minor) >= (3, 11), (
        f"pinned {python['version']} below backend minimum"
    )
    assert str(python["url"]).startswith("https://")
    assert SHA256_RE.match(python["sha256"]), "python.sha256 must be 64 lowercase hex"


def test_wheels_well_formed(lock):
    wheels = lock["wheels"]
    assert len(wheels) > 0
    seen = set()
    for wheel in wheels:
        for field in ("name", "version", "filename", "url", "sha256"):
            assert wheel.get(field), f"wheel missing {field}: {wheel}"
        assert str(wheel["url"]).startswith("https://"), wheel["name"]
        assert SHA256_RE.match(wheel["sha256"]), f"bad sha256: {wheel['name']}"
        # filename must agree with name+version (PEP 427/503)
        dist = wheel["filename"].split("-")[0]
        assert _norm(dist) == _norm(wheel["name"]), (
            f"filename/name mismatch: {wheel['filename']}"
        )
        assert f"-{wheel['version']}-" in wheel["filename"], (
            f"filename/version mismatch: {wheel['filename']}"
        )
        key = _norm(wheel["name"])
        assert key not in seen, f"duplicate wheel: {wheel['name']}"
        seen.add(key)


def test_closure_covers_pyproject_runtime_deps(lock):
    """Every [project] dependency must appear in the locked closure."""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    pinned = {_norm(w["name"]) for w in lock["wheels"]}
    missing = []
    for dep in pyproject["project"]["dependencies"]:
        name = re.split(r"[<>=!;\s\[]", dep, maxsplit=1)[0]
        if _norm(name) not in pinned:
            missing.append(name)
    assert not missing, f"lockfile missing runtime deps: {missing}"
