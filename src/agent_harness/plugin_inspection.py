"""Static checks for the pinned DSH reasoning-effort slider adapter."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_harness.skills.inspection import _read_bounded_package_text

_PACKAGE_NAME = "dsh-codex-effort-slider"
_PINNED_VERSION = "1.0.0"
_PINNED_FILES = (
    "LICENSE",
    "package.json",
    "cordis.patch.yml",
    "install-profile.ps1",
    "install.cmd",
    "pack-dist.ps1",
    "lib/index.js",
    "lib/client.js",
)
_PINNED_FILES_SHA256 = "67035efef55780aaf649ff47959d3f4eface8b55d0c9dea1b80b6f089d8e881e"
_FILE_LIMITS = {"package.json": 64_000, "lib/client.js": 512_000}
_SOURCE_PIN = {
    "repository": "https://github.com/Microqian2th/dsh-codex-effort-slider",
    "commit": "af723caf3387e64ae28aa69c4fd235b1b662e3ae",
    "tree": "8a732b7c00ba5da3b06e122c243205db4ab190b1",
    "archive_sha256": "122917c83671d5e2ec9f759875db071d47cf738c5774f0810a9d428d20ccce0c",
    "checked_files_sha256": _PINNED_FILES_SHA256,
}


def inspect_dsh_effort_slider(source: Path | str, *, scope: str = "project") -> dict[str, Any] | None:
    """Report the DSH slider's project-side mapping without importing package code.

    ``None`` means the directory is not this known package, so the caller may
    continue with its existing Skill inspection path.
    """
    source_path = Path(source).expanduser()
    try:
        root = source_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not root.is_dir():
        return None

    errors: list[dict[str, str]] = []
    package_text = _read_bounded_package_text(
        root,
        root / "package.json",
        "package.json",
        _FILE_LIMITS["package.json"],
        errors,
        too_large_code="PACKAGE_FILE_TOO_LARGE",
        unreadable_code="PACKAGE_FILE_UNREADABLE",
        changed_code="PACKAGE_FILE_CHANGED",
        not_file_code="PACKAGE_FILE_NOT_A_FILE",
        outside_code="PACKAGE_FILE_OUTSIDE_SOURCE",
    )
    if package_text is None:
        return None
    try:
        manifest = json.loads(package_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(manifest, dict) or manifest.get("name") != _PACKAGE_NAME:
        return None

    findings: list[dict[str, str]] = []
    source_files: dict[str, bytes] = {}
    for relative in _PINNED_FILES:
        if relative == "package.json":
            source_files[relative] = package_text.encode("utf-8").replace(b"\r\n", b"\n")
            continue
        file_errors: list[dict[str, str]] = []
        package_file = root / relative
        if not package_file.exists():
            findings.append({
                "code": "MISSING_CLIENT_ENTRY" if relative == "lib/client.js" else "MISSING_REQUIRED_FILE",
                "path": relative,
            })
            continue
        text = _read_bounded_package_text(
            root,
            package_file,
            relative,
            _FILE_LIMITS.get(relative, 128_000),
            file_errors,
            too_large_code="PACKAGE_FILE_TOO_LARGE",
            unreadable_code="PACKAGE_FILE_UNREADABLE",
            changed_code="PACKAGE_FILE_CHANGED",
            not_file_code="PACKAGE_FILE_NOT_A_FILE",
            outside_code="PACKAGE_FILE_OUTSIDE_SOURCE",
        )
        if text is None:
            findings.append({"code": file_errors[0]["code"], "path": relative})
            continue
        source_files[relative] = text.encode("utf-8").replace(b"\r\n", b"\n")

    version = manifest.get("version")
    license_name = manifest.get("license")
    if version != _PINNED_VERSION:
        findings.append({
            "code": "SOURCE_VERSION_DRIFT",
            "expected": _PINNED_VERSION,
            "actual": str(version),
        })
    if license_name != "MIT":
        findings.append({"code": "LICENSE_DRIFT", "expected": "MIT", "actual": str(license_name)})

    expected_dsh = {
        "bundle": {"patch": "./cordis.patch.yml"},
        "client": {"platform": "web", "immediately": True},
    }
    exports = manifest.get("exports")
    if (
        manifest.get("main") != "./lib/index.js"
        or not isinstance(exports, dict)
        or exports.get("./client") != "./lib/client.js"
        or manifest.get("dsh") != expected_dsh
    ):
        findings.append({"code": "PACKAGE_CONTRIBUTION_DRIFT", "path": "package.json"})

    observed_digest: str | None = None
    if len(source_files) == len(_PINNED_FILES):
        digest = hashlib.sha256()
        for relative in _PINNED_FILES:
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update(source_files[relative] + b"\0")
        observed_digest = digest.hexdigest()
        if observed_digest != _PINNED_FILES_SHA256:
            findings.append({"code": "SOURCE_CONTENT_DRIFT", "expected": _PINNED_FILES_SHA256, "actual": observed_digest})

    license_file = source_files.get("LICENSE", b"").decode("utf-8", errors="replace")
    if license_file and not license_file.startswith("MIT License"):
        findings.append({"code": "LICENSE_FILE_MISMATCH", "path": "LICENSE"})

    # `off` is an upstream request value. Project Default maps to None, which
    # intentionally omits the wire field; these behaviors cannot be equated.
    gaps = [{
        "name": "off",
        "status": "unmapped",
        "reason": "The native model catalog supports minimal/standard/deep; Default omits the request field.",
        "evidence": [
            "src/agent_harness/model/config.py:95,108-117",
            "src/agent_harness/model/provider.py:95-102",
            "web/src/components/ReasoningEffortSlider.tsx:89-115,122-125",
        ],
    }]
    return {
        "status": "needs-adaptation",
        "source": str(root),
        "scope": scope,
        "package": _PACKAGE_NAME,
        "version": version,
        "license": license_name or "未声明",
        "source_pin": dict(_SOURCE_PIN),
        "source_pin_matches": not findings and observed_digest == _PINNED_FILES_SHA256,
        "observed_checked_files_sha256": observed_digest,
        "dependencies": [
            {"name": "DSH", "required_by_source": True, "evidence": "package.json:dsh.client"},
            {"name": "Cordis", "required_by_source": True, "evidence": "package.json:dsh.bundle.patch"},
            {"name": "DSH client UI / private DOM", "required_by_source": True, "evidence": "pinned lib/client.js"},
        ],
        "contributions": [
            {
                "name": "Model-specific effort options and default",
                "source": "lib/client.js:183-225",
                "project_mapping": "Model catalog capabilities + native Composer slider",
                "status": "adapted",
                "evidence": ["src/agent_harness/model/config.py:108-163", "web/src/components/Composer.tsx:413-429"],
            },
            {
                "name": "Select and send an effort value",
                "source": "lib/client.js:1117-1121",
                "project_mapping": "Provider validates the selected level and applies its wire mapping",
                "status": "adapted",
                "evidence": ["src/agent_harness/model/provider.py:95-104", "tests/model/test_reasoning_effort.py:40-45,71-73"],
            },
            {
                "name": "Keyboard and accessible slider interaction",
                "source": "lib/client.js:1220-1234,1298-1312",
                "project_mapping": "Native range input with keyboard and aria labels",
                "status": "adapted",
                "evidence": ["web/src/components/ReasoningEffortSlider.tsx:126-137,181-194"],
            },
            {
                "name": "Theme and reduced motion",
                "source": "README.md:233-245",
                "project_mapping": "Project themes and prefers-reduced-motion CSS",
                "status": "adapted",
                "note": "Project suppresses motion while upstream slows its decorative animation.",
                "evidence": ["web/src/styles/app.css:7570-7580,7758-7780"],
            },
        ],
        "gaps": gaps,
        "findings": findings,
        "execution": {
            "package_code_run": False,
            "dsh_started": False,
            "private_dom_accessed": False,
            "second_model_state_created": False,
            "activation_allowed": False,
        },
        "activation_allowed": False,
    }
