from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_harness.sandbox.windows_psec as psec


def _stub_archive_handles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure: str | None,
) -> list[int]:
    monkeypatch.setattr(
        psec,
        "os",
        SimpleNamespace(name="nt", fspath=os.fspath),
    )
    monkeypatch.setattr(
        psec,
        "_open_directory_chain",
        lambda *args, **kwargs: [1],
    )
    opened = iter((2, 3))
    monkeypatch.setattr(
        psec,
        "_open_relative_entry",
        lambda *args, **kwargs: next(opened),
    )
    monkeypatch.setattr(psec, "_file_type", lambda handle: psec._FILE_TYPE_DISK)

    def file_attributes(handle: int) -> int:
        if handle == 3 and failure == "attributes":
            raise OSError("injected attribute query failure")
        if handle == 3 and failure == "reparse":
            return psec._FILE_ATTRIBUTE_REPARSE_POINT
        return 0

    def file_standard_info(handle: int) -> SimpleNamespace:
        if handle == 3 and failure == "standard-info":
            raise OSError("injected standard-info query failure")
        return SimpleNamespace(Directory=True)

    closed: list[int] = []
    monkeypatch.setattr(psec, "_file_attributes", file_attributes)
    monkeypatch.setattr(psec, "_file_standard_info", file_standard_info)
    monkeypatch.setattr(psec, "_directory_entry_names", lambda handle: [])
    monkeypatch.setattr(psec, "_close_handles", lambda handles: closed.extend(handles))
    return closed


@pytest.mark.parametrize("failure", ["attributes", "standard-info", "reparse"])
def test_archive_pin_closes_second_handle_when_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    closed = _stub_archive_handles(monkeypatch, failure=failure)
    expected_error = PermissionError if failure == "reparse" else OSError

    with pytest.raises(expected_error), psec._pin_host_path_tree_for_archive(
        Path(r"C:\project")
    ):
        pytest.fail("invalid archive entry was accepted")

    assert closed.count(3) == 1
    assert closed.count(2) == 1
    assert closed.count(1) == 1


def test_archive_pin_keeps_second_handle_until_context_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = _stub_archive_handles(monkeypatch, failure=None)

    with psec._pin_host_path_tree_for_archive(Path(r"C:\project")) as (
        entries,
        transferred,
    ):
        assert entries == [("", 3, True)]
        assert 3 not in closed
        assert transferred == set()

    assert closed.count(3) == 1
    assert closed.count(2) == 1
    assert closed.count(1) == 1
