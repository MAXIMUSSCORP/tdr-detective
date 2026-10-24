"""query_events temp files: bounded retention, private directory, path in the manifest."""

from __future__ import annotations

import os
import stat
import time
from datetime import timedelta
from pathlib import Path

import pytest

from pc_diag_mcp.tools import SavedEventsResult, query_events
from pc_diag_mcp.tools import events as events_module
from pc_diag_mcp.tools.events import (
    TEMPFILE_MAX_AGE_S,
    TEMPFILE_MAX_COUNT,
    save_events_tempfile,
)

from .test_tools import T0, StubCollector


def _old_file(directory: Path, name: str, age_s: float) -> Path:
    path = directory / name
    path.write_text("[]")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_files_older_than_an_hour_are_removed(tmp_path: Path) -> None:
    directory = tmp_path / "tdr-detective"
    directory.mkdir(mode=0o700)
    stale = _old_file(directory, "events-stale.json", TEMPFILE_MAX_AGE_S + 60)
    fresh = _old_file(directory, "events-fresh.json", 60)
    other = _old_file(directory, "notes.txt", TEMPFILE_MAX_AGE_S + 60)
    new = save_events_tempfile([], tmp_path)
    assert not stale.exists()
    assert fresh.exists() and new.exists()
    assert other.exists()  # only our own events-*.json files are managed


def test_file_count_is_capped_oldest_first(tmp_path: Path) -> None:
    directory = tmp_path / "tdr-detective"
    directory.mkdir(mode=0o700)
    files = [_old_file(directory, f"events-{i:03}.json", 600 - i) for i in range(60)]
    new = save_events_tempfile([], tmp_path)
    remaining = sorted(directory.glob("events-*.json"))
    assert len(remaining) == TEMPFILE_MAX_COUNT == 50
    assert new in remaining
    # the oldest (lowest index = largest age) went first
    assert not any(f.exists() for f in files[:11]) and all(f.exists() for f in files[11:])


def test_reparse_points_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "tdr-detective"
    directory.mkdir(mode=0o700)
    real_lstat = os.lstat

    class _Info:
        def __init__(self, info: os.stat_result) -> None:
            self._info = info
            self.st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

        def __getattr__(self, name: str) -> object:
            return getattr(self._info, name)

    monkeypatch.setattr(
        events_module.os,
        "lstat",
        lambda p: _Info(real_lstat(p)) if Path(p) == directory else real_lstat(p),
    )
    with pytest.raises(PermissionError, match="reparse point"):
        save_events_tempfile([], tmp_path)


def test_saved_path_is_in_manifest_notes(tmp_path: Path) -> None:
    result = query_events(
        StubCollector(), T0, T0 + timedelta(hours=1), save_to="tempfile", temp_dir=tmp_path
    )
    assert isinstance(result, SavedEventsResult)
    assert any(result.path in note for note in result.collection.notes)
