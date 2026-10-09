"""Prepare the offline Python runtime bundled into the Windows installer.

Executable form of installer/README.md -> "Preparing the Python runtime": read
the pinned lockfile, download and verify the CPython archive and the wheel
closure, extract the interpreter under desktop/installer/staging/, build the
product wheel from this checkout (`uv build --wheel`) and install everything
offline into the staged runtime.

Usage:
    python desktop/scripts/prepare_python_runtime.py [--cache DIR] [--repo DIR]

Exit code 0 means desktop/installer/staging/python/python.exe imports the
product at the pinned version — the same assertion
scripts/build-windows-installer.mjs enforces in afterPack (W-21 defect D2:
the closure alone is dependencies only, so the bundled service had nothing to
run).

Idempotent: a download with a matching SHA-256 and an already-installed wheel
set are skipped, so a re-run after a source change only rebuilds the product
wheel. Requires `uv` on PATH (the build backend pinned in pyproject.toml).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_RELATIVE = Path("desktop") / "installer" / "python-runtime.lock.json"
STAGING_RELATIVE = Path("desktop") / "installer" / "staging"
DEFAULT_CACHE = REPO_ROOT / ".scratch" / "python-runtime-cache"
CLOSURE_MARKER = "installed-closure.txt"

PROBE = (
    "import importlib, importlib.metadata as m;"
    "mod = importlib.import_module({module!r} + '.cli');"
    "assert callable(mod.main), {module!r} + '.cli:main is not callable';"
    "print(m.version({name!r}))"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, dest: Path, expected_sha256: str) -> Path:
    """Download url to dest unless a file with the expected hash is already there."""
    if dest.exists() and sha256_file(dest) == expected_sha256:
        print(f"[skip] {dest.name} (sha256 ok)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    print(f"[get ] {url}")
    with urllib.request.urlopen(url, timeout=300) as response, partial.open("wb") as out:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    actual = sha256_file(partial)
    if actual != expected_sha256:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"sha256 mismatch for {dest.name}: got {actual}, want {expected_sha256}")
    partial.replace(dest)
    print(f"[ok  ] {dest.name} sha256 verified")
    return dest


def ensure_interpreter(lock: dict, cache: Path, staging: Path) -> Path:
    """Download + verify + extract the pinned CPython; return python.exe."""
    pin = lock["python"]
    python_exe = staging / "python" / "python.exe"
    if python_exe.exists():
        print(f"[skip] staged interpreter present: {python_exe}")
        return python_exe
    filename = Path(pin["url"].split("/")[-1].replace("%2B", "+")).name
    archive = download(pin["url"], cache / "downloads" / filename, pin["sha256"])
    staging.mkdir(parents=True, exist_ok=True)
    print(f"[xtr ] {archive.name} -> {staging}")
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(staging)  # the archive's top-level directory is python/
    if not python_exe.exists():
        raise SystemExit(f"extraction did not produce {python_exe}")
    return python_exe


def build_product_wheel(repo: Path, product: dict, wheelhouse: Path) -> Path:
    """Build the product wheel from this checkout and check it against the pin."""
    expected = wheelhouse / product["wheel"]
    print(f"[uv  ] uv build --wheel --out-dir {wheelhouse}")
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheelhouse)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stdout.write(result.stdout)
        raise SystemExit(f"uv build failed (exit {result.returncode}):\n{result.stderr.strip()}")
    if not expected.exists():
        pattern = product["name"].replace("-", "_") + "-*"
        produced = sorted(path.name for path in wheelhouse.glob(pattern))
        raise SystemExit(
            f"{expected.name} was not produced for {product['name']}=={product['version']} "
            f"(wheelhouse has: {produced or 'no product wheel'}) — update product.name / "
            "product.version / product.wheel in the lockfile to match pyproject.toml"
        )
    print(f"[ok  ] {expected.name} sha256 {sha256_file(expected)}")
    return expected


def install_offline(python_exe: Path, wheelhouse: Path, requirements: list[str], marker: Path) -> None:
    """pip install the locked dependency closure, with no index access."""
    recorded = "\n".join(requirements)
    if marker.exists() and marker.read_text(encoding="utf-8") == recorded:
        print("[skip] wheel closure already installed")
        return
    command = [
        str(python_exe),
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-warn-script-location",
        "--find-links",
        str(wheelhouse),
        *requirements,
    ]
    print(f"[pip ] installing {len(requirements)} distributions offline")
    subprocess.run(command, check=True)
    marker.write_text(recorded, encoding="utf-8")


def install_product(python_exe: Path, wheel: Path) -> None:
    """Install the product wheel over whatever is staged already.

    `--force-reinstall` is the point: a rebuilt product keeps the same version
    string, so pip's "requirement already satisfied" check would silently keep the
    previous code in the runtime the installer ships (observed: the staged runtime
    still had the pre-fix `web/app.py`). `--no-deps` keeps it to the one
    distribution — the closure is handled above.
    """
    print(f"[pip ] installing {wheel.name} (force-reinstall, no deps)")
    subprocess.run(
        [
            str(python_exe),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--force-reinstall",
            "--no-warn-script-location",
            str(wheel),
        ],
        check=True,
    )


def probe_product(python_exe: Path, product: dict) -> str:
    """Import the product's CLI in the staged runtime; return its version."""
    probe = PROBE.format(module=product["module"], name=product["name"])
    result = subprocess.run(
        [str(python_exe), "-c", probe], text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise SystemExit(f"staged runtime does not import {product['name']}:\n{result.stderr.strip()}")
    version = result.stdout.strip().splitlines()[-1].strip()
    if version != product["version"]:
        raise SystemExit(f"staged {product['name']} is {version}, lockfile pins {product['version']}")
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage the offline Python runtime for the Windows installer.")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="checkout to build from")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="download cache (gitignored by default)")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    cache = Path(args.cache).resolve()
    lock = json.loads((repo / LOCK_RELATIVE).read_text(encoding="utf-8"))
    wheels = lock["wheels"]
    product = lock["product"]
    print(
        f"lock: cpython {lock['python']['version']} + {len(wheels)} wheels "
        f"+ {product['name']}=={product['version']}"
    )

    staging = repo / STAGING_RELATIVE
    wheelhouse = cache / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)

    python_exe = ensure_interpreter(lock, cache, staging)
    for wheel in wheels:
        download(wheel["url"], wheelhouse / wheel["filename"], wheel["sha256"])
    product_wheel = build_product_wheel(repo, product, wheelhouse)
    requirements = [f"{wheel['name']}=={wheel['version']}" for wheel in wheels]
    install_offline(python_exe, wheelhouse, requirements, staging / "python" / CLOSURE_MARKER)
    install_product(python_exe, product_wheel)

    version = probe_product(python_exe, product)
    print(f"[done] {python_exe} imports {product['module']} ({product['name']}=={version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
