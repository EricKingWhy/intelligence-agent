"""Restricted Python worker transport for the existing Docker backend.

PORT DESIGN: Codex computation-only isolate / Anthropic no-egress container.
The workspace volume is deliberately absent. Effects can only cross stdin/stdout
to the caller's tool dispatcher. This is Docker backend plumbing, not a second
Tool runtime. All frames, including forged frames, are untrusted input.
"""

from __future__ import annotations

import json
import socket
import struct
from pathlib import Path
from typing import Any

MAX_FRAME_BYTES = 4 * 1024 * 1024


class DockerCodeWorker:
    def __init__(self, client: Any, image: str, owner: str) -> None:
        self._client = client
        self._image = image
        self._owner = owner
        self._container = None
        self._socket = None
        self._buffer = bytearray()

    def start(self, code: str, output_chars: int) -> None:
        runner = Path(__file__).with_name('code_worker_runner.py').read_text(encoding='utf-8')
        self._container = self._client.containers.run(
            self._image, ['timeout', '--signal=KILL', '300', 'python', '-I', '-B', '-u', '-c', runner],
            detach=True, stdin_open=True, network_mode='none', read_only=True,
            tmpfs={'/tmp': 'rw,noexec,nosuid,nodev,size=64m,mode=1777'},
            working_dir='/tmp', user='65534:65534', cap_drop=['ALL'],
            security_opt=['no-new-privileges:true'], pids_limit=32,
            mem_limit='256m', memswap_limit='256m', nano_cpus=1_000_000_000,
            environment={'PYTHONDONTWRITEBYTECODE': '1'}, use_config_proxy=False,
            labels={'agent-harness.role': 'code-worker', 'agent-harness.owner': self._owner},
        )
        self._socket = self._container.attach_socket(
            params={'stdin': 1, 'stdout': 1, 'stderr': 1, 'stream': 1},
        )
        self.send({'code': code, 'output_chars': output_chars})

    def send(self, frame: dict) -> None:
        raw = (json.dumps(frame, ensure_ascii=True) + '\n').encode('utf-8')
        if len(raw) > MAX_FRAME_BYTES:
            raise ValueError('Code bridge frame exceeds 4 MiB')
        stream = getattr(self._socket, '_sock', self._socket)
        stream.sendall(raw)

    def _read_exact(self, size: int) -> bytes:
        pieces = bytearray()
        while len(pieces) < size:
            chunk = self._socket.read(size - len(pieces))
            if not chunk:
                raise EOFError('Code worker closed its bridge')
            pieces.extend(chunk)
        return bytes(pieces)

    def receive(self) -> dict:
        # Docker's non-TTY attach stream uses an 8-byte multiplex header.
        while b'\n' not in self._buffer:
            header = self._read_exact(8)
            size = struct.unpack('>I', header[4:])[0]
            if size > MAX_FRAME_BYTES or len(self._buffer) + size > MAX_FRAME_BYTES:
                raise ValueError('Code bridge frame exceeds 4 MiB')
            payload = self._read_exact(size)
            if header[0] == 2:
                raise RuntimeError(payload[:2000].decode('utf-8', errors='replace'))
            if header[0] != 1:
                raise ValueError('Unexpected Docker stream channel')
            self._buffer.extend(payload)
        line, _, remainder = self._buffer.partition(b'\n')
        self._buffer = bytearray(remainder)
        frame = json.loads(line)
        if not isinstance(frame, dict):
            raise TypeError('Code bridge frame must be an object')
        return frame

    def close(self) -> None:
        try:
            if self._container is not None:
                # Force removal waits for the whole container to stop, including
                # subprocesses left behind by a script. No workspace lock is held.
                self._container.remove(force=True)
        finally:
            if self._socket is not None:
                stream = getattr(self._socket, '_sock', self._socket)
                try:
                    stream.shutdown(socket.SHUT_RDWR)
                except (OSError, AttributeError):
                    pass
                response = getattr(self._socket, '_response', None)
                if response is not None:
                    response.close()
                self._socket.close()
