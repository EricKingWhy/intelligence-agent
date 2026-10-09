#!/usr/bin/env python3
"""#830 / MM-08 跨端与恢复验证驱动（**只验证，不改造产品**）。

跑法（在 worktree 根，venv 的 bin 必须在 PATH 最前）：

    export no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1
    export PATH="$HOME/workspace/intelligence-agent/.venv/bin:$PATH"
    export PYTHONPATH="$PWD/src"
    python scripts/verify_830_mm08.py --work /home/hatch/pytest-830/mm08 --out /home/hatch/pytest-830/mm08/evidence.json

它拉起 `scripts/mm08_stub_server.py`（**真实 uvicorn**：真实 HTTP、真实会话 JSONL、
真实 Local 附件字节；只有模型 provider 是确定性替身），然后逐条验 AC1–AC7。
AC6 的「真实视觉模型回答」在本机无凭证，恒记 NOT_RUN（不 mock 冒充）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from typing import Any

import httpx

REPO = pathlib.Path(__file__).resolve().parents[1]
STUB = REPO / "scripts" / "mm08_stub_server.py"

CATALOG = json.dumps(
    [
        {
            "name": "vision-probe",
            "provider": "deepseek",
            "model_name": "deepseek-chat",
            "supports_vision": True,
        },
        {
            "name": "mm08-summary",
            "provider": "deepseek",
            "model_name": "mm08-summary-model",
            "supports_vision": False,
        },
    ],
    ensure_ascii=False,
)
SUMMARY_ENTRY = "mm08-summary"
SUMMARY_NAME = "mm08-summary-model"

_TERMINAL_RUN_TYPES = {
    "run/completed",
    "run/paused",
    "run/failed",
    "run/cancelled",
}
_BASE64_RUN = re.compile(rb"[A-Za-z0-9+/]{200,}")
_CLI_WRAPPER = """\
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.environ["MM08_SCRIPTS"])
import mm08_stub_server as stub

import agent_harness.assembly as asm
from agent_harness.model import provider as prov

sink = os.environ.get("MM08_MODEL_SINK") or None
summary_name = os.environ.get("MM08_SUMMARY_NAME", "")
delay = int(os.environ.get("MM08_MODEL_DELAY_MS", "0"))


def factory(config, **kwargs):
    role = "summary" if getattr(config, "model_name", None) == summary_name else "main"
    return stub.VerifyModel(sink, role, delay)


with patch.object(asm, "create_chat_model", factory), patch.object(
    prov, "create_chat_model", factory
):
    from agent_harness.cli import main

    main()
"""


# --------------------------------------------------------------------------- #
# 图片字节构造（与 tests/attachments/helpers.py 同形，但独立实现，避免 scripts→tests 耦合）
# --------------------------------------------------------------------------- #
def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)
    )


def png_bytes(width: int, height: int) -> bytes:
    """头部合法的最小 PNG（本仓只解析图片头，不解码，故负载可为压缩零）。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    out = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
    out += _png_chunk(b"IDAT", zlib.compress(b"\x00" + b"\x00" * (width * 3)))
    out += _png_chunk(b"IEND", b"")
    return out


def large_png_bytes(width: int, height: int, payload_bytes: int) -> bytes:
    """头部合法、体积很大的 PNG（AC5「事件不爆炸」用）。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    out = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
    out += _png_chunk(b"IDAT", os.urandom(payload_bytes))
    out += _png_chunk(b"IEND", b"")
    return out


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
class Report:
    """证据收集器：每条 check 落 `status/evidence/notes`，最后写 JSON。"""

    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self._log_path: pathlib.Path | None = None

    def bind_log(self, path: pathlib.Path) -> None:
        self._log_path = path

    def log(self, message: str) -> None:
        print(message, flush=True)
        if self._log_path is not None:
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(message + "\n")

    def add(
        self,
        check_id: str,
        name: str,
        status: str,
        evidence: dict[str, Any] | None = None,
        notes: list[str] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": check_id,
            "name": name,
            "status": status,
            "evidence": evidence or {},
            "notes": notes or [],
        }
        self.checks.append(entry)
        self.log(f"[{status}] {check_id} {name}")
        for line in entry["notes"]:
            self.log(f"       note: {line}")
        return entry

    def fail(self, check_id: str, name: str, why: str, **extra: Any) -> dict[str, Any]:
        return self.add(check_id, name, "FAIL", {"error": why, **extra})


# --------------------------------------------------------------------------- #
# 服务进程
# --------------------------------------------------------------------------- #
class Server:
    """`scripts/mm08_stub_server.py` 的子进程封装（可 kill / 重启同一数据目录）。"""

    def __init__(
        self, root: pathlib.Path, port: int, *, delay_ms: int = 0, tag: str = "srv"
    ) -> None:
        self.root = root
        self.port = port
        self.delay_ms = delay_ms
        self.tag = tag
        self.proc: subprocess.Popen[bytes] | None = None
        self.log_path = root / f"{tag}.log"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "MM08_WORKSPACE_DIR": str(self.root / "ws"),
                "MM08_ARTIFACT_DIR": str(self.root / "art"),
                "MM08_AGENT_MODELS": CATALOG,
                "MM08_SUMMARY_MODEL": SUMMARY_ENTRY,
                "MM08_SUMMARY_NAME": SUMMARY_NAME,
                "MM08_MODEL_NAME": "vision-probe",
                "MM08_MODEL_PROVIDER": "deepseek",
                "MM08_MODEL_SINK": str(self.root / "model-sink.jsonl"),
                "MM08_MODEL_DELAY_MS": str(self.delay_ms),
                "MM08_PORT": str(self.port),
                "PYTHONPATH": str(REPO / "src"),
                "no_proxy": "localhost,127.0.0.1",
                "NO_PROXY": "localhost,127.0.0.1",
            }
        )
        return env

    def start(self) -> None:
        handle = self.log_path.open("ab")
        self.proc = subprocess.Popen(
            [sys.executable, str(STUB)],
            cwd=str(REPO),
            env=self.env(),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def wait_ready(self, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        with httpx.Client(timeout=5.0) as probe:
            while time.monotonic() < deadline:
                try:
                    if probe.get(f"{self.base_url}/api/health").status_code == 200:
                        return
                except httpx.HTTPError:
                    # 启动窗口内连接被拒是预期，不是失败信号。
                    pass
                time.sleep(0.4)
        raise RuntimeError(f"server not ready: {self.base_url}")

    def kill(self) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        self.proc = None

    def restart(self) -> None:
        self.kill()
        self.start()
        self.wait_ready()

    @property
    def sink(self) -> pathlib.Path:
        return self.root / "model-sink.jsonl"

    def session_jsonl(self, session_id: str) -> pathlib.Path:
        return self.root / "ws" / "sessions" / session_id / "events.jsonl"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# HTTP 客户端小工具
# --------------------------------------------------------------------------- #
def new_client(server: Server, timeout: float = 120.0) -> httpx.Client:
    return httpx.Client(base_url=server.base_url, timeout=timeout)


def create_session(client: httpx.Client, task: str) -> str:
    """`POST /api/sessions`（SSE）：从帧里取 session_id。"""
    response = client.post(
        "/api/sessions",
        json={
            "task": task,
            "model": "vision-probe",
            "budget": {"local": {"max_agent_turns": 8}},
        },
    )
    response.raise_for_status()
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        frame = json.loads(line[5:].strip())
        if isinstance(frame, dict) and frame.get("session_id"):
            return str(frame["session_id"])
    raise RuntimeError("SSE 里没有 session_id")


def events(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/sessions/{session_id}/events")
    response.raise_for_status()
    return list(response.json())


def settled(rows: list[dict[str, Any]], *, after_seq: int = -1) -> bool:
    return any(
        row["type"] in _TERMINAL_RUN_TYPES and row["seq"] > after_seq for row in rows
    )


def wait_settled(client: httpx.Client, session_id: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if settled(events(client, session_id)):
            return
        time.sleep(0.3)
    raise RuntimeError(f"session {session_id} 未在 {timeout}s 内落终态")


def upload(
    client: httpx.Client, session_id: str, payload: bytes, name: str
) -> dict[str, Any]:
    response = client.post(
        f"/api/sessions/{session_id}/attachments",
        content=payload,
        headers={"content-type": "application/octet-stream"},
        params={"name": name},
    )
    response.raise_for_status()
    return dict(response.json())


def send_with_image(
    client: httpx.Client, session_id: str, content: str, ref: str
) -> int:
    response = client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": content, "attachments": [ref]},
    )
    wait_settled(client, session_id)
    return response.status_code


def read_content(client: httpx.Client, session_id: str, ref: str) -> httpx.Response:
    return client.get(f"/api/sessions/{session_id}/attachments/{ref}/content")


def normalized_not_found(text: str, ids: list[str]) -> str:
    """把 404 回执里**请求方自己给出的** id 归一成占位符，再比对是否同形。

    响应体回显请求 id 不构成泄露（请求方原本就知道它）；判据是除该 id 外**逐字同形**：
    同一个模板、同一个会话名、同一个原因串。若某一路径额外泄露了"这个 id 存在"
    之类的信息，归一后仍会不同。
    """
    out = text
    for value in ids:
        out = out.replace(value, "<id>")
    return out


def refs_of(rows: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for row in rows:
        if row["type"] != "user/message":
            continue
        for item in row["data"].get("attachments") or []:
            if isinstance(item, dict) and item.get("attachment_id"):
                found.append(str(item["attachment_id"]))
    return found


def sha256_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# 第二/第三客户端
# --------------------------------------------------------------------------- #
_TUI_SCRIPT = """\
import { writeFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const zlib = require("node:zlib");
const [repoRoot, baseUrl, mode, sessionId, outPath, name] = process.argv.slice(2);
const { ApiClient } = await import(`${repoRoot}/tui/src/api.ts`);
const out = { mode };
const attachmentRefs = (rows) => {
  const refs = [];
  for (const row of rows) {
    if (row?.type !== "user/message") continue;
    for (const item of row?.data?.attachments ?? []) {
      if (item?.attachment_id) refs.push(item.attachment_id);
    }
  }
  return refs;
};
const readBack = async (sid, ref) => {
  const url = `${baseUrl}/api/sessions/${sid}/attachments/${ref}/content`;
  const response = await fetch(url);
  const buf = new Uint8Array(await response.arrayBuffer());
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return {
    status: response.status,
    bytes: buf.byteLength,
    sha256: [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join(""),
  };
};

try {
  const api = new ApiClient(baseUrl);
  if (mode === "read") {
    const rows = await api.getEvents(sessionId);
    out.eventCount = rows.length;
    out.refs = attachmentRefs(rows);
    if (out.refs.length > 0) Object.assign(out, await readBack(sessionId, out.refs[0]));
  } else if (mode === "write") {
    const created = await api.createSession();
    const sid = created.session_id;
    out.sessionId = sid;
    const receipt = await api.uploadAttachment(sid, new Uint8Array(minimalPng()), {
      name: name ?? "tui.png",
      mediaType: "image/png",
    });
    out.receipt = receipt;
    await api.sendMessage(sid, "TUI 附图 [Image #1]", [receipt.attachment_id]);
    out.refs = [];
    for (let attempt = 0; attempt < 40; attempt += 1) {
      const rows = await api.getEvents(sid);
      out.refs = attachmentRefs(rows);
      if (out.refs.length > 0) { out.eventCount = rows.length; break; }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    if (out.refs.length > 0) Object.assign(out, await readBack(sid, out.refs[0]));
  }
} catch (error) {
  out.err = String(error);
}
writeFileSync(outPath, JSON.stringify(out));

function minimalPng() {
  const crcTable = [];
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    crcTable[n] = c >>> 0;
  }
  const crc = (buf) => {
    let c = 0xffffffff;
    for (const byte of buf) c = crcTable[(c ^ byte) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (type, payload) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(payload.length);
    const body = Buffer.concat([Buffer.from(type, "ascii"), payload]);
    const sum = Buffer.alloc(4);
    sum.writeUInt32BE(crc(body));
    return Buffer.concat([len, body, sum]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(8, 0);
  ihdr.writeUInt32BE(6, 4);
  ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(Buffer.alloc(1 + 8 * 3))),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}
"""


def run_tui_client(
    work: pathlib.Path, mode: str, base_url: str, session_id: str, name: str = "tui.png"
) -> dict[str, Any]:
    """跑**真实 TUI 客户端代码**（`tui/src/api.ts::ApiClient`）去打真实服务。"""
    script = work / "tui_client.mjs"
    if not script.exists():
        script.write_text(_TUI_SCRIPT, encoding="utf-8")
    out_path = work / f"tui-{mode}-out.json"
    result = subprocess.run(
        [
            "node",
            "--experimental-transform-types",
            str(script),
            str(REPO),
            base_url,
            mode,
            session_id,
            str(out_path),
            name,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(work),
        check=False,
    )
    if not out_path.exists():
        return {
            "err": "node 未产出结果",
            "exit": result.returncode,
            "stderr": result.stderr[-2000:],
        }
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    payload["node_exit"] = result.returncode
    return payload


def run_cli(
    work: pathlib.Path,
    root: pathlib.Path,
    message: str,
    images: list[str],
    *,
    sink: pathlib.Path,
) -> dict[str, Any]:
    """把真 CLI（`agent_harness.cli:main`）跑成**独立子进程**（真 argparse/真落盘）。"""
    wrapper = work / "cli_wrapper.py"
    if not wrapper.exists():
        wrapper.write_text(_CLI_WRAPPER, encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "MM08_SCRIPTS": str(REPO / "scripts"),
            "MM08_MODEL_SINK": str(sink),
            "MM08_SUMMARY_NAME": SUMMARY_NAME,
            "MM08_MODEL_DELAY_MS": "0",
            "WORKSPACE_DIR": str(root / "ws"),
            "ARTIFACT_DIR": str(root / "art"),
            "MODEL_API_KEY": "sk-mm08-verify",
            "MODEL_PROVIDER": "deepseek",
            "MODEL_NAME": "vision-probe",
            "AGENT_MODELS": CATALOG,
            "PYTHONPATH": str(REPO / "src"),
            "no_proxy": "localhost,127.0.0.1",
            "NO_PROXY": "localhost,127.0.0.1",
        }
    )
    argv = [sys.executable, str(wrapper), message]
    for image in images:
        argv += ["--image", image]
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        cwd=str(root),
        check=False,
    )
    return {
        "exit": result.returncode,
        "stdout_tail": result.stdout[-1500:],
        "stderr_tail": result.stderr[-1500:],
    }


def cli_sessions(root: pathlib.Path) -> list[pathlib.Path]:
    sessions_root = root / "ws" / "sessions"
    if not sessions_root.is_dir():
        return []
    return sorted(
        (path for path in sessions_root.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
    )


def cli_events(root: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for session_dir in cli_sessions(root):
        log = session_dir / "events.jsonl"
        if not log.exists():
            continue
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _image_blocks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从替身模型的请求快照里挑出图片块（含降级后的占位文本块证据）。"""
    found: list[dict[str, Any]] = []
    for row in rows:
        for message in row.get("messages", []):
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") in (
                    "image",
                    "image_url",
                ):
                    found.append(block)
    return found


def sink_rows(server: Server) -> list[dict[str, Any]]:
    if not server.sink.exists():
        return []
    rows = []
    for line in server.sink.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


# --------------------------------------------------------------------------- #
# AC 1-7
# --------------------------------------------------------------------------- #
def ac1_cross_client(rep: Report, server: Server, work: pathlib.Path) -> dict[str, Any]:
    """AC1：Web 端图文 → 另一客户端（真实 TUI ApiClient）看到同一 ref 与同一字节。"""
    client = new_client(server)
    try:
        sid = create_session(client, "AC1 跨端一致")
        payload = png_bytes(8, 6)
        receipt = upload(client, sid, payload, "ac1.png")
        status = send_with_image(client, sid, "看这张图 [Image #1]", receipt["attachment_id"])
        rows = events(client, sid)
        refs = refs_of(rows)
        got = read_content(client, sid, receipt["attachment_id"])
        client_hash = sha256_of(got.content)
        tui = run_tui_client(work, "read", server.base_url, sid)
        main_requests = [row for row in sink_rows(server) if row["role"] == "main"]
        provider_blocks = _image_blocks(main_requests)
        evidence = {
            "session_id": sid,
            "web_send_status": status,
            "attachment_id": receipt["attachment_id"],
            "expected_sha256": sha256_of(payload),
            "web_read_status": got.status_code,
            "web_read_sha256": client_hash,
            "web_event_refs": refs,
            "tui_event_refs": tui.get("refs"),
            "tui_read_status": tui.get("status"),
            "tui_read_sha256": tui.get("sha256"),
            "tui_event_count": tui.get("eventCount"),
            "tui_node_exit": tui.get("node_exit"),
            "provider_request_image_blocks": provider_blocks,
            "provider_request_image_block_count": len(provider_blocks),
        }
        ok = (
            status == 200
            and refs == [receipt["attachment_id"]]
            and tui.get("refs") == refs
            and tui.get("status") == 200
            and tui.get("sha256") == client_hash
            and client_hash == sha256_of(payload)
            and len(provider_blocks) > 0
        )
        notes = []
        if not ok:
            notes.append(f"tui_err={tui.get('err')}")
        return rep.add(
            "AC1",
            "同一 session：Web 上传/引用 → TUI 客户端读到同一 attachment 引用与同一字节",
            "PASS" if ok else "FAIL",
            evidence,
            notes,
        )
    finally:
        client.close()


def ac2_lifecycle(
    rep: Report, server: Server, work: pathlib.Path, session_id: str, ref: str
) -> dict[str, Any]:
    """AC2：SIGKILL 重启后事件与附件字节稳态；在途 run 被 kill 后事实不丢、可恢复。"""
    client = new_client(server)
    try:
        before_rows = events(client, session_id)
        before_read = read_content(client, session_id, ref)
        before_hash = sha256_of(before_read.content)
        before_jsonl = server.session_jsonl(session_id).read_bytes()

        server.restart()

        after_rows = events(client, session_id)
        after_read = read_content(client, session_id, ref)
        after_jsonl = server.session_jsonl(session_id).read_bytes()
        same_events = [row["event_id"] for row in before_rows] == [
            row["event_id"] for row in after_rows
        ]

        # ② 在途 run 被 SIGKILL 打断 → 重启 → 用 resume 接上后续聊；引用与字节都不丢
        server.delay_ms = 4000
        server.restart()
        in_flight: dict[str, Any] = {}

        def _send() -> None:
            try:
                inner = new_client(server, timeout=8.0)
                in_flight["status"] = inner.post(
                    f"/api/sessions/{session_id}/messages",
                    json={"content": "在途被打断 [Image #1]", "attachments": [ref]},
                ).status_code
                inner.close()
            except Exception as error:  # noqa: BLE001 —— 打断窗口里连接被切是预期
                in_flight["error"] = type(error).__name__

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()
        time.sleep(1.5)
        server.kill()
        thread.join(timeout=20)

        server.delay_ms = 0
        server.start()
        server.wait_ready()

        crashed_rows = events(client, session_id)
        crashed_user = [
            row
            for row in crashed_rows
            if row["type"] == "user/message" and refs_of([row])
        ]
        crashed_seq = int(crashed_user[-1]["seq"]) if crashed_user else -1
        crashed_read = read_content(client, session_id, ref)

        resume = client.post(
            f"/api/sessions/{session_id}/resume", json={"task": "崩溃后继续"}
        )
        resumed = False
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            rows = events(client, session_id)
            if any(
                row["type"] in _TERMINAL_RUN_TYPES and row["seq"] > crashed_seq
                for row in rows
            ):
                resumed = True
                break
            time.sleep(0.4)
        post_rows = events(client, session_id)
        post_read = read_content(client, session_id, ref)

        evidence = {
            "session_id": session_id,
            "event_count_before": len(before_rows),
            "event_count_after_restart": len(after_rows),
            "event_ids_stable": same_events,
            "jsonl_bytes_stable": before_jsonl == after_jsonl,
            "jsonl_bytes_before": len(before_jsonl),
            "content_sha256_before": before_hash,
            "content_sha256_after_restart": sha256_of(after_read.content),
            "content_status_after_restart": after_read.status_code,
            "in_flight_request": in_flight,
            "in_flight_user_message_persisted": bool(crashed_user),
            "in_flight_user_message_seq": crashed_seq,
            "content_status_after_crash": crashed_read.status_code,
            "content_sha256_after_crash": sha256_of(crashed_read.content),
            "resume_status": resume.status_code,
            "resume_body": resume.text[:200],
            "new_terminal_run_after_resume": resumed,
            "event_count_after_resume": len(post_rows),
            "content_status_after_resume": post_read.status_code,
            "content_sha256_after_resume": sha256_of(post_read.content),
        }
        ok = (
            same_events
            and before_jsonl == after_jsonl
            and after_read.status_code == 200
            and sha256_of(after_read.content) == before_hash
            and evidence["in_flight_user_message_persisted"]
            and crashed_read.status_code == 200
            and sha256_of(crashed_read.content) == before_hash
            and resume.status_code == 200
            and resumed
            and post_read.status_code == 200
            and sha256_of(post_read.content) == before_hash
        )
        return rep.add(
            "AC2",
            "kill -9 / 重启：事件与附件字节稳态；在途 run 被打断后事实不丢且可 resume 接续",
            "PASS" if ok else "FAIL",
            evidence,
        )
    finally:
        client.close()


def ac3_fork(rep: Report, server: Server, work: pathlib.Path) -> dict[str, Any]:
    """AC3：fork 边界**之后**的 child 应能复用边界之前的图（Artifact Ref 复用语义）。

    「能复用」在本仓的可观测面有两个：(a) 受控读回端点能取回同一字节；
    (b) 送给模型的装配结果里该图仍是图片块（不是占位符降级）。
    """
    client = new_client(server)
    try:
        sid = create_session(client, "AC3 fork 边界")
        payload = png_bytes(10, 4)
        receipt = upload(client, sid, payload, "ac3.png")
        ref = receipt["attachment_id"]
        send_with_image(client, sid, "第一张图 [Image #1]", ref)
        for index in (2, 3):
            client.post(
                f"/api/sessions/{sid}/messages", json={"content": f"第 {index} 轮（无图）"}
            )
            wait_settled(client, sid)

        rows = events(client, sid)
        # 同 AC4：必须选**带这张图的那条** user/message，`user_rows[0]` 是建会话的任务消息。
        image_rows = [
            row for row in rows if row["type"] == "user/message" and ref in refs_of([row])
        ]
        image_event_id = image_rows[0]["event_id"]
        image_seq = int(image_rows[0]["seq"])
        # `from_seq` 必须是 **user/message 的 seq**（`web/lineage.py` 契约）：取最后一条用户轮。
        user_rows = [row for row in rows if row["type"] == "user/message"]
        last_seq = int(user_rows[-1]["seq"])

        after = client.post(f"/api/sessions/{sid}/forks", json={"from_seq": last_seq})
        after.raise_for_status()
        child_id = after.json()["session_id"]
        child_rows = events(client, child_id)
        child_refs = refs_of(child_rows)
        child_read = read_content(client, child_id, ref)

        # child 侧的「图仍可解释」：再跑一轮，看装配给模型的是图片块还是占位符。
        sink_before = len(sink_rows(server))
        client.post(
            f"/api/sessions/{child_id}/messages", json={"content": "child 继续 [Image #1]"}
        )
        wait_settled(client, child_id)
        child_requests = sink_rows(server)[sink_before:]
        child_blocks = _image_blocks(child_requests)
        child_placeholder = any(
            "image omitted" in json.dumps(row, ensure_ascii=False)
            for row in child_requests
        )

        at = client.post(f"/api/sessions/{sid}/forks", json={"from_seq": image_seq})
        at.raise_for_status()
        boundary_id = at.json()["session_id"]
        boundary_rows = events(client, boundary_id)
        boundary_read = read_content(client, boundary_id, ref)

        parent_ids = {row["event_id"] for row in rows}
        evidence = {
            "parent_session_id": sid,
            "image_event_id": image_event_id,
            "image_user_seq": image_seq,
            "fork_from_seq": last_seq,
            "child_session_id": child_id,
            "child_event_count": len(child_rows),
            "child_refs": child_refs,
            "child_image_event_present_by_id": image_event_id
            in {row["event_id"] for row in child_rows},
            "child_read_status": child_read.status_code,
            "child_read_body": child_read.text[:160]
            if child_read.status_code != 200
            else None,
            "child_read_sha256": sha256_of(child_read.content)
            if child_read.status_code == 200
            else None,
            "expected_sha256": sha256_of(payload),
            "child_model_request_image_blocks": child_blocks,
            "child_model_request_has_placeholder": child_placeholder,
            "child_artifact_dir_exists": (
                server.root / "art" / child_id
            ).is_dir(),
            "parent_artifact_object": str(
                server.root / "art" / sid / "attachments" / "objects"
            ),
            "boundary_fork_session_id": boundary_id,
            "boundary_refs": refs_of(boundary_rows),
            "boundary_read_status": boundary_read.status_code,
            "child_has_session_forked": any(
                row["type"] == "session/forked" for row in child_rows
            ),
            "parent_has_session_forked": any(
                row["type"] == "session/forked" for row in rows
            ),
            "child_event_ids_subset_of_parent": all(
                row["event_id"] in parent_ids for row in child_rows
            ),
            "parent_event_count_unchanged": len(events(client, sid)) == len(rows),
        }
        # 边界语义（图在边界之前 → child 继承引用）与边界排除（图正好在边界 → 不继承）
        boundary_ok = (
            image_event_id in {row["event_id"] for row in child_rows}
            and child_refs == [ref]
            and refs_of(boundary_rows) == []
            and boundary_read.status_code == 404
            and evidence["child_has_session_forked"]
            and not evidence["parent_has_session_forked"]
            and evidence["parent_event_count_unchanged"]
        )
        explicable = (
            child_read.status_code == 200
            and not child_placeholder
            and len(child_blocks) > 0
        )
        notes = []
        if boundary_ok and not explicable:
            notes.append(
                "缺陷候选（AC3）：child 的 user/message 继承了 attachment ref，"
                "但附件字节在 child 命名空间里取不回（LocalArtifactStore 按 session_id "
                "分目录，fork 不复制/不链接附件对象）⇒ 受控读回 404，且装配给模型的"
                "图片块被降级成 '(image omitted: model does not support images)' 占位符。"
            )
        return rep.add(
            "AC3",
            "fork 边界语义与图复用：child 继承边界前的 ref，且该图在 child 仍可取回/可解释",
            "PASS" if (boundary_ok and explicable) else "FAIL",
            evidence,
            notes,
        )
    finally:
        client.close()


def ac4_compaction(rep: Report, server: Server) -> dict[str, Any]:
    """AC4：压缩后摘要保留 artifact ref、旧图可找回、事实不被删除。"""
    client = new_client(server)
    try:
        sid = create_session(client, "AC4 压缩保留引用")
        payload = png_bytes(12, 9)
        receipt = upload(client, sid, payload, "ac4.png")
        ref = receipt["attachment_id"]
        send_with_image(client, sid, "压缩前的图 [Image #1]", ref)
        for index in range(2, 5):
            client.post(
                f"/api/sessions/{sid}/messages", json={"content": f"第 {index} 轮"}
            )
            wait_settled(client, sid)

        jsonl = server.session_jsonl(sid)
        raw_before = jsonl.read_text(encoding="utf-8")
        rows_before = events(client, sid)
        # 必须选**带这张图的那条** user/message：`user_rows[0]` 是建会话的那条任务消息
        # （无 attachments），用它会让「事实未被改写」这条断言退化成恒真/恒假。
        image_rows = [
            row for row in rows_before if row["type"] == "user/message" and ref in refs_of([row])
        ]
        image_event_id = image_rows[0]["event_id"]
        image_event_seq = image_rows[0]["seq"]

        # run 终结的收尾窗口按 `is_busy` 语义算「忙」（runmanager 明令），409 是
        # 设计内的瞬时拒绝 —— 按真实客户端行为重试，而不是把它当压缩失败。
        compact_attempts = 0
        for _ in range(30):
            compact_attempts += 1
            compaction = client.post(f"/api/sessions/{sid}/context/compact")
            if compaction.status_code == 200:
                break
            time.sleep(1.0)
        body = compaction.json() if compaction.status_code == 200 else {}
        rows_after = events(client, sid)
        summary_rows = [row for row in rows_after if row["type"] == "context/compacted"]
        summary = summary_rows[-1]["data"]["summary"] if summary_rows else ""
        raw_after = jsonl.read_text(encoding="utf-8")
        image_event_after = [
            row for row in rows_after if row["event_id"] == image_event_id
        ]
        read_after = read_content(client, sid, ref)

        summary_requests = [
            row for row in sink_rows(server) if row["role"] == "summary"
        ]
        transcript_blob = json.dumps(summary_requests, ensure_ascii=False)

        evidence = {
            "session_id": sid,
            "compact_status": compaction.status_code,
            "compact_attempts": compact_attempts,
            "compact_body": body,
            "summary_event_count": len(summary_rows),
            "summary_schema": summary_rows[-1]["data"].get("schema") if summary_rows else None,
            "summary_contains_ref": ref in summary,
            "summary_ref_section": [
                block.strip()
                for block in summary.split("## 精确标识清单")[-1].splitlines()
                if block.strip()
            ][:3]
            if "## 精确标识清单" in summary
            else [],
            "summary_has_base64": bool(_BASE64_RUN.search(summary.encode("utf-8"))),
            "summary_transcript_has_file_id": ref in transcript_blob,
            "summary_transcript_has_base64": bool(
                _BASE64_RUN.search(transcript_blob.encode("utf-8"))
            ),
            "pre_compaction_user_message_still_present": bool(image_event_after),
            "pre_compaction_user_message_unmodified": bool(image_event_after)
            and refs_of(image_event_after) == [ref],
            "pre_compaction_image_event_seq": image_event_seq,
            "jsonl_grew_only": len(raw_after) > len(raw_before),
            "jsonl_bytes_before": len(raw_before),
            "jsonl_bytes_after": len(raw_after),
            "content_status_after_compaction": read_after.status_code,
            "content_sha256_after_compaction": sha256_of(read_after.content),
            "expected_sha256": sha256_of(payload),
        }
        ok = (
            compaction.status_code == 200
            and body.get("compacted_turn_count", 0) > 0
            and evidence["summary_contains_ref"]
            and not evidence["summary_has_base64"]
            and evidence["summary_transcript_has_file_id"]
            and not evidence["summary_transcript_has_base64"]
            and evidence["pre_compaction_user_message_still_present"]
            and evidence["pre_compaction_user_message_unmodified"]
            and read_after.status_code == 200
            and sha256_of(read_after.content) == sha256_of(payload)
        )
        return rep.add(
            "AC4",
            "压缩后摘要保留 artifact ref（精确标识清单）；旧图可找回；事实不被压缩删除",
            "PASS" if ok else "FAIL",
            evidence,
        )
    finally:
        client.close()


def ac5_no_base64(rep: Report, server: Server) -> dict[str, Any]:
    """AC5：事件流不含 base64 图片数据；历史图与压缩不造成事件爆炸。"""
    client = new_client(server)
    try:
        sid = create_session(client, "AC5 事件不带 base64")
        small = png_bytes(8, 6)
        small_receipt = upload(client, sid, small, "ac5-small.png")
        send_with_image(client, sid, "小图 [Image #1]", small_receipt["attachment_id"])
        jsonl = server.session_jsonl(sid)
        bytes_before_big = len(jsonl.read_bytes())
        count_before_big = len(events(client, sid))

        big = large_png_bytes(320, 240, 512 * 1024)
        big_receipt = upload(client, sid, big, "ac5-big.png")
        send_with_image(client, sid, "大图 [Image #1]", big_receipt["attachment_id"])
        raw = jsonl.read_bytes()
        count_after_big = len(events(client, sid))

        rows = events(client, sid)
        user_events = [row for row in rows if row["type"] == "user/message"]
        evidence = {
            "session_id": sid,
            "big_image_bytes": len(big),
            "jsonl_bytes_before_big": bytes_before_big,
            "jsonl_bytes_after_big": len(raw),
            "jsonl_growth_bytes": len(raw) - bytes_before_big,
            "jsonl_growth_ratio": round((len(raw) - bytes_before_big) / len(big), 6),
            "jsonl_has_base64_run": bool(_BASE64_RUN.search(raw)),
            "jsonl_base64_run_count": len(_BASE64_RUN.findall(raw)),
            "events_before_big": count_before_big,
            "events_after_big": count_after_big,
            "event_growth": count_after_big - count_before_big,
            "attachment_meta_recorded": [
                {
                    key: item.get(key)
                    for key in ("attachment_id", "media_type", "bytes", "width", "height")
                }
                for item in (user_events[-1]["data"].get("attachments") or [])
            ],
            "artifact_bytes_on_disk": len(big),
            "event_stream_base64_runs": len(_BASE64_RUN.findall(raw)),
            "provider_request_image_blocks": _image_blocks(
                [row for row in sink_rows(server) if row["role"] == "main"]
            ),
        }
        ok = (
            not evidence["jsonl_has_base64_run"]
            and evidence["jsonl_growth_bytes"] < 8192
            and evidence["jsonl_growth_ratio"] < 0.02
            and len(evidence["provider_request_image_blocks"]) > 0
        )
        return rep.add(
            "AC5",
            "事件流只记引用/尺寸/mime，不含 base64；大图不造成事件爆炸",
            "PASS" if ok else "FAIL",
            evidence,
        )
    finally:
        client.close()


def ac6_clients(rep: Report, server: Server, work: pathlib.Path) -> list[dict[str, Any]]:
    """AC6：三端上传附图；真实视觉模型回答 = NOT_RUN（无凭证，不 mock）。"""
    results: list[dict[str, Any]] = []
    # ① TUI 客户端的**写**路径（真实 `ApiClient.uploadAttachment/sendMessage`）
    tui = run_tui_client(work, "write", server.base_url, "unused", "ac6-tui.png")
    tui_ref = (tui.get("refs") or [None])[0]
    tui_receipt = tui.get("receipt") or {}
    tui_ok = (
        bool(tui_ref)
        and tui_ref == tui_receipt.get("attachment_id")
        and tui.get("status") == 200
        and tui.get("bytes") == tui_receipt.get("bytes")
    )
    results.append(
        rep.add(
            "AC6-TUI",
            "真实 TUI 客户端代码（tui/src/api.ts）上传附图 → 事件带引用 → 回读同字节",
            "PASS" if tui_ok else "FAIL",
            {
                "session_id": tui.get("sessionId"),
                "receipt": tui_receipt,
                "event_refs": tui.get("refs"),
                "read_status": tui.get("status"),
                "read_bytes": tui.get("bytes"),
                "node_exit": tui.get("node_exit"),
                "err": tui.get("err"),
            },
        )
    )

    # ② CLI（独立子进程：真 argparse / 真落盘）
    results.append(ac6_cli(rep, work, server.sink))

    # ③ 真实视觉模型的回答：本机无凭证 → NOT_RUN
    results.append(
        rep.add(
            "AC6-VISION",
            "真实视觉模型对同一张图给出回答（服务端真跑）",
            "NOT_RUN",
            {
                "reason": (
                    "仓库内无任何模型凭证：无 .env、无 MODEL_* 环境变量；"
                    "`model_supports_vision` 只有 `mimo` provider preset 声明 "
                    "supports_vision=true，需 MiMo key（缺失）。凭证缺失时**不 mock 冒充**，"
                    "按缺口①记 NOT_RUN。"
                ),
                "what_was_instead_verified": (
                    "真实 uvicorn + 真实 HTTP + 真实落盘 + 确定性替身模型："
                    "附件上传/引用/回读/跨端/跨生命周期/授权/压缩全部真跑。"
                ),
            },
        )
    )
    return results


def ac6_cli(rep: Report, work: pathlib.Path, sink: pathlib.Path) -> dict[str, Any]:
    """AC6-CLI 单跑：真 CLI 子进程的成功/失败路径。

    与服务器解耦（只借 `sink` 这个**文件路径**，不碰 stub server 进程），因为
    Windows 近似验证（`.github/workflows/mm08-windows-verify.yml`）要在一个没有
    uvicorn 服务的 runner 上复用同一份断言，而不是另写一份。
    """
    cli_root = work / "cli"
    cli_root.mkdir(parents=True, exist_ok=True)
    good = cli_root / "good.png"
    good.write_bytes(png_bytes(8, 6))
    missing = cli_root / "missing.png"
    bad_text = cli_root / "bad.txt"
    bad_text.write_text("not an image", encoding="utf-8")
    mismatched = cli_root / "mismatched.png"
    mismatched.write_bytes(b"definitely not a png")

    ok_run = run_cli(work, cli_root, "CLI 附图", [str(good)], sink=sink)
    miss_run = run_cli(work, cli_root, "CLI 缺图", [str(missing)], sink=sink)
    bad_run = run_cli(work, cli_root, "CLI 非图片", [str(bad_text)], sink=sink)
    mismatch_run = run_cli(work, cli_root, "CLI 扩展名不符", [str(mismatched)], sink=sink)
    cli_rows = cli_events(cli_root)
    cli_refs = refs_of(cli_rows)
    good_hash = sha256_of(good.read_bytes())
    expected_ref = f"sha256:{good_hash}"
    cli_ok = (
        ok_run["exit"] == 0
        and expected_ref in cli_refs
        and miss_run["exit"] == 1
        and bad_run["exit"] == 1
        and mismatch_run["exit"] == 1
        and len(cli_refs) == 1
    )
    return rep.add(
        "AC6-CLI",
        "CLI `--image`：成功 exit 0 且事件带引用；缺文件/非图片/扩展名不符 exit 1 且零附件",
        "PASS" if cli_ok else "FAIL",
        {
            "success_exit": ok_run["exit"],
            "success_stdout_tail": ok_run["stdout_tail"][-400:],
            "missing_exit": miss_run["exit"],
            "missing_stderr_tail": miss_run["stderr_tail"][-200:],
            "non_image_exit": bad_run["exit"],
            "non_image_stderr_tail": bad_run["stderr_tail"][-200:],
            "mismatched_ext_exit": mismatch_run["exit"],
            "mismatched_ext_stderr_tail": mismatch_run["stderr_tail"][-200:],
            "cli_event_refs": cli_refs,
            "expected_ref": expected_ref,
            "cli_attachment_objects": sorted(
                str(path) for path in (cli_root / "art").rglob("*") if path.is_file()
            ),
        },
    )


def ac7_authorization(rep: Report, server: Server) -> dict[str, Any]:
    """AC7：跨会话读附件 → 404（与"不存在"不可区分）；同会话未引用 → 404；形状非法 → 422。"""
    client = new_client(server)
    try:
        owner = create_session(client, "AC7 属主会话")
        payload = png_bytes(9, 5)
        receipt = upload(client, owner, payload, "ac7.png")
        ref = receipt["attachment_id"]
        send_with_image(client, owner, "属主引用 [Image #1]", ref)

        other = create_session(client, "AC7 旁观会话")
        # 旁观会话引用**自己**的、字节不同的图（若复用同一份字节，内容寻址会让
        # 它同样归属该会话、跨会话读回本就应当 200 —— 那样测不到授权闸门）。
        other_payload = png_bytes(7, 3)
        referenced_elsewhere = upload(client, other, other_payload, "ac7b.png")
        send_with_image(
            client,
            other,
            "旁观会话自己引用一张 [Image #1]",
            referenced_elsewhere["attachment_id"],
        )
        # 同一份字节只上传、**不发送**：本会话「未引用」也应当 404。
        unreferenced = upload(client, other, png_bytes(4, 4), "ac7-unreferenced.png")
        same_bytes_uploaded_only = upload(client, other, payload, "ac7-same-bytes.png")

        cross = read_content(client, other, ref)
        absent = read_content(client, other, "sha256:" + "0" * 64)
        unreferenced_read = read_content(client, other, unreferenced["attachment_id"])
        malformed = read_content(client, other, "sha256:nothex")
        owned = read_content(client, owner, ref)

        upload_hijack = client.post(
            f"/api/sessions/{other}/attachments",
            content=payload,
            headers={"content-type": "application/octet-stream"},
            params={"name": "hijack.png"},
        )
        evidence = {
            "owner_session": owner,
            "other_session": other,
            "attachment_id": ref,
            "cross_session_status": cross.status_code,
            "cross_session_body": cross.text[:200],
            "absent_status": absent.status_code,
            "absent_body": absent.text[:200],
            "same_session_unreferenced_status": unreferenced_read.status_code,
            "same_session_unreferenced_body": unreferenced_read.text[:200],
            "same_session_unreferenced_sha256": unreferenced["attachment_id"],
            "same_bytes_uploaded_only_id": same_bytes_uploaded_only["attachment_id"],
            "same_bytes_uploaded_only_status": read_content(
                client, other, same_bytes_uploaded_only["attachment_id"]
            ).status_code,
            "malformed_status": malformed.status_code,
            "malformed_body": malformed.text[:160],
            "owner_read_status": owned.status_code,
            "owner_read_sha256": sha256_of(owned.content),
            "expected_sha256": sha256_of(payload),
            "cross_body_identical_to_absent": normalized_not_found(
                cross.text, [ref, "sha256:" + "0" * 64]
            )
            == normalized_not_found(absent.text, [ref, "sha256:" + "0" * 64]),
            "cross_body_identical_to_unreferenced": normalized_not_found(
                cross.text, [ref, unreferenced["attachment_id"]]
            )
            == normalized_not_found(
                unreferenced_read.text, [ref, unreferenced["attachment_id"]]
            ),
            "reupload_same_bytes_yields_same_id": upload_hijack.json().get(
                "attachment_id"
            )
            == ref,
            "other_session_own_image_sha256": sha256_of(
                read_content(client, other, referenced_elsewhere["attachment_id"]).content
            ),
            "other_session_own_read_status": read_content(
                client, other, referenced_elsewhere["attachment_id"]
            ).status_code,
        }
        ok = (
            cross.status_code == 404
            and absent.status_code == 404
            and unreferenced_read.status_code == 404
            and malformed.status_code == 422
            and owned.status_code == 200
            and sha256_of(owned.content) == sha256_of(payload)
            and evidence["cross_body_identical_to_absent"]
            and evidence["cross_body_identical_to_unreferenced"]
            and evidence["same_bytes_uploaded_only_status"] == 404
            and evidence["other_session_own_read_status"] == 200
            and evidence["other_session_own_image_sha256"] == sha256_of(other_payload)
        )
        return rep.add(
            "AC7",
            "真实服务上跨会话读附件 404（与不存在不可区分）；未引用 404；形状非法 422",
            "PASS" if ok else "FAIL",
            evidence,
        )
    finally:
        client.close()


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="#830 / MM-08 验证驱动")
    parser.add_argument("--work", default="/home/hatch/pytest-830/mm08")
    parser.add_argument("--out", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--only",
        default="all",
        choices=("all", "ac6-cli"),
        help="只跑一段；ac6-cli 不需要 uvicorn 服务（Windows 近似验证复用同一份断言）",
    )
    args = parser.parse_args()

    work = pathlib.Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    out = pathlib.Path(args.out) if args.out else work / "evidence.json"

    rep = Report()
    rep.bind_log(work / "verify.log")
    rep.log(f"repo={REPO}")
    rep.log(f"work={work}")
    rep.log(f"python={sys.executable}")

    if args.only == "ac6-cli":
        sink = work / "cli-sink.jsonl"
        rep.log(f"only=ac6-cli sink={sink}")
        ac6_cli(rep, work, sink)
        return write_summary(rep, out, work=work, server_log=None, model_sink=sink)

    server = Server(work, args.port or free_port())
    started = time.monotonic()
    try:
        server.start()
        server.wait_ready()
        rep.log(f"server ready at {server.base_url} (pid={server.proc.pid})")

        acl = ac1_cross_client(rep, server, work)
        ref = acl["evidence"].get("attachment_id")
        sid = acl["evidence"].get("session_id")
        if ref and sid:
            ac2_lifecycle(rep, server, work, sid, ref)
        else:
            rep.fail("AC2", "kill/restart 稳态", "AC1 未产出可用 session/ref")
        ac3_fork(rep, server, work)
        ac4_compaction(rep, server)
        ac5_no_base64(rep, server)
        ac6_clients(rep, server, work)
        ac7_authorization(rep, server)
    finally:
        server.kill()
        rep.log(f"server killed; elapsed={time.monotonic() - started:.1f}s")

    return write_summary(
        rep, out, work=work, server_log=server.log_path, model_sink=server.sink
    )


def write_summary(
    rep: Report,
    out: pathlib.Path,
    *,
    work: pathlib.Path,
    server_log: pathlib.Path | None,
    model_sink: pathlib.Path,
) -> int:
    """落盘证据 JSON 并以「有无 FAIL」定退出码（0 = 无 FAIL）。"""
    summary = {
        "issue": 830,
        "ticket": "MM-08 跨端与恢复验证",
        "repo": str(REPO),
        "work": str(work),
        "server_log": str(server_log) if server_log is not None else None,
        "model_sink": str(model_sink),
        "checks": rep.checks,
        "counts": {
            status: sum(1 for check in rep.checks if check["status"] == status)
            for status in ("PASS", "FAIL", "NOT_RUN")
        },
    }
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    rep.log(f"counts={summary['counts']}")
    rep.log(f"evidence -> {out}")
    return 0 if summary["counts"]["FAIL"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
