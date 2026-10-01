import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
payload = json.loads((ROOT / "docs/.codex-issue346-write.json").read_text(encoding="utf-8"))

for item in payload["changes"]:
    path = ROOT / item["path"]
    marker = item["marker"].encode("utf-8")
    text = item["text"]
    before = path.read_bytes() if path.exists() else b""
    if marker in before:
        raise SystemExit(f"refusing duplicate marker: {item['path']}")
    if path.exists() and before:
        crlf = before.count(b"\r\n")
        lf = before.count(b"\n") - crlf
        eol = b"\r\n" if crlf > lf else b"\n"
    else:
        eol = b"\n"
    addition = text.replace("\r\n", "\n").replace("\n", eol.decode("ascii")).encode("utf-8")
    prefix = before
    if prefix and not prefix.endswith((b"\n", b"\r")):
        prefix += eol
    expected = prefix + addition + eol
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as stream:
        stream.write(expected)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    actual = path.read_bytes()
    if actual != expected or not actual.startswith(before) or actual.count(marker) != 1 or not actual.endswith(eol):
        raise SystemExit(f"byte verification failed: {item['path']}")
    blob = subprocess.run(
        ["git", "hash-object", "--", str(path.relative_to(ROOT))],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"{item['path']} bytes={len(actual)} sha256={hashlib.sha256(actual).hexdigest()} blob={blob}")

