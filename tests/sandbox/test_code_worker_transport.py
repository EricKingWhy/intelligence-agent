"""Unit tests for Docker Code Worker stream transport compatibility."""

from types import SimpleNamespace

import pytest

from agent_harness.sandbox.code_worker import DockerCodeWorker


class _PartialReadStream:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.requested_sizes: list[int] = []

    def read(self, size: int) -> bytes:
        self.requested_sizes.append(size)
        chunk, self._payload = self._payload[:2], self._payload[2:]
        return chunk


class _PartialRecvStream:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.requested_sizes: list[int] = []

    def recv(self, size: int) -> bytes:
        self.requested_sizes.append(size)
        chunk, self._payload = self._payload[:2], self._payload[2:]
        return chunk


@pytest.mark.parametrize("stream_type", [_PartialReadStream, _PartialRecvStream])
def test_read_exact_supports_partial_file_and_socket_reads(stream_type):
    stream = stream_type(b"hello")
    worker = DockerCodeWorker(client=None, image="", owner="")
    worker._socket = SimpleNamespace(_sock=stream)

    assert worker._read_exact(5) == b"hello"
    assert stream.requested_sizes == [5, 3, 1]
