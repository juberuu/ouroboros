"""Phase 4 (v6.39) G: crash-safe atomic full-file overwrite."""

from __future__ import annotations

import pytest

from ouroboros import utils
from ouroboros.utils import atomic_write_json, write_text, write_text_atomic


def test_write_text_atomic_writes_content(tmp_path):
    target = tmp_path / "f.txt"
    write_text_atomic(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"


def test_write_text_atomic_preserves_old_file_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "f.txt"
    target.write_text("OLD CONTENT", encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("simulated crash during replace")

    # Fail the atomic swap AFTER the temp is written: the EXISTING file must stay fully
    # intact (never a truncated/partial file) and the orphan temp must be cleaned up.
    monkeypatch.setattr(utils.os, "replace", _boom)
    with pytest.raises(OSError):
        write_text_atomic(target, "NEW CONTENT THAT NEVER LANDS")

    assert target.read_text(encoding="utf-8") == "OLD CONTENT"
    assert not list(tmp_path.glob(".f.txt.tmp.*"))  # no orphaned temp left behind


@pytest.mark.skipif(__import__("sys").platform.startswith("win"),
                    reason="POSIX execute bits are not preserved/reported on Windows")
def test_write_text_atomic_preserves_full_mode(tmp_path):
    import os
    target = tmp_path / "script.sh"
    target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    # setgid + rwxr-x--- exercises the FULL 0o7777 mask (special bits, not just rwx). Use
    # whatever the filesystem actually stored as the baseline so the test is fs-robust.
    os.chmod(target, 0o2750)
    expected = os.stat(target).st_mode & 0o7777
    # os.replace creates a new inode; the existing mode (incl any special bits) must survive.
    write_text_atomic(target, "#!/bin/sh\necho new\n")
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\necho new\n"
    assert (os.stat(target).st_mode & 0o7777) == expected
    assert (os.stat(target).st_mode & 0o111)  # still executable


def test_write_text_helper_is_atomic(tmp_path):
    # utils.write_text (the shared overwrite helper used by git.py et al.) now routes
    # through the atomic primitive.
    target = tmp_path / "g.txt"
    target.write_text("OLD", encoding="utf-8")
    write_text(target, "NEW")
    assert target.read_text(encoding="utf-8") == "NEW"


def test_atomic_write_json_still_works(tmp_path):
    target = tmp_path / "d.json"
    atomic_write_json(target, {"a": 1, "b": [2, 3]})
    import json
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}
