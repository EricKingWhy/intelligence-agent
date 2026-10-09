"""Prepare the offline Node runtime bundled into the Windows installer.

Mirror of prepare_python_runtime.py for the terminal client's runtime: read
desktop/installer/node-runtime.lock.json, download the pinned nodejs.org zip,
verify its SHA-256, and extract node.exe (plus the archive's license) under
desktop/installer/staging/node/.

Why the artifact ships a second runtime (W-21 D5 / #817): the TUI is a raw-mode
terminal application, and the app's own Electron binary cannot host one.
Measured on Electron 44.5.1: with ELECTRON_RUN_AS_NODE=1 `process.stdin.isTTY`
is undefined, `setRawMode` does not exist, and reopening fd 0/1/2 through
node:tty fails with ERR_TTY_INIT_FAILED — while the same console hands a real
Node the same file descriptors as a TTY. So ia-tui.cmd runs the staged node.exe.

Usage:
    python desktop/scripts/prepare_node_runtime.py [--cache DIR] [--repo DIR]

Exit code 0 means desktop/installer/staging/node/node.exe reports the pinned
version — the same assertion scripts/build-windows-installer.mjs enforces in
afterPack. Idempotent: a download with the matching SHA-256 and an
already-extracted member are skipped.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path

# One verified-download implementation for both runtimes (sha256 check + cache).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_python_runtime import download

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_RELATIVE = Path("desktop") / "installer" / "node-runtime.lock.json"
STAGING_RELATIVE = Path("desktop") / "installer" / "staging"
DEFAULT_CACHE = REPO_ROOT / ".scratch" / "node-runtime-cache"


def extract_members(archive: Path, members: list[str], node_dir: Path) -> None:
    """Extract pinned members into node_dir, flattening the version directory."""
    with zipfile.ZipFile(archive) as bundle:
        for member in members:
            target = node_dir / Path(member).name
            if target.exists():
                print(f"[skip] staged {target.name} present")
                continue
            with bundle.open(member) as source, target.open("wb") as out:
                out.write(source.read())
            print(f"[xtr ] {member} -> {target.name}")


def probe(node_exe: Path, version: str) -> str:
    """Run the staged binary; return its --version output if it matches the pin."""
    result = subprocess.run([str(node_exe), "--version"], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"staged node does not start: {node_exe}\n{result.stderr.strip()}")
    actual = result.stdout.strip()
    if actual != f"v{version}":
        raise SystemExit(f"staged node reports {actual}, node-runtime.lock.json pins v{version}")
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage the offline Node runtime for the Windows installer.")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="checkout to stage into")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="download cache (gitignored by default)")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    cache = Path(args.cache).resolve()
    lock = json.loads((repo / LOCK_RELATIVE).read_text(encoding="utf-8"))
    pin = lock["node"]
    print(f"lock: node {pin['version']} ({pin['sha256'][:12]}...)")

    node_dir = repo / STAGING_RELATIVE / lock["layout"]["resourcesDir"]
    node_dir.mkdir(parents=True, exist_ok=True)
    archive = download(pin["url"], cache / "downloads" / pin["archive"], pin["sha256"])
    extract_members(archive, pin["members"], node_dir)

    node_exe = repo / STAGING_RELATIVE / Path(lock["layout"]["executable"])
    print(f"[done] {probe(node_exe, pin['version'])} at {node_exe}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
