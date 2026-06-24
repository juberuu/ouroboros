"""Regression tests for passive managed-update status cache overlay."""

import ouroboros.gateway.control as control
import supervisor.git_ops as git_ops
import supervisor.state as supervisor_state


def _base_status(current_sha: str) -> dict:
    return {
        "managed": True,
        "current_sha": current_sha,
        "current_short_sha": current_sha[:8],
        "current_branch": "ouroboros",
        "target_ref": "managed/ouroboros",
        "dirty": False,
        "available": False,
        "safe_to_apply": False,
        "warnings": ["official_status_requires_check"],
    }


def _wire_status(monkeypatch, *, current_sha: str, latest_sha: str, target_is_ancestor: bool = False) -> None:
    monkeypatch.setattr(git_ops, "compute_managed_update_status", lambda fetch=False: _base_status(current_sha))
    monkeypatch.setattr(
        supervisor_state,
        "load_state",
        lambda: {
            "managed_update_cache": {
                "available": True,
                "safe_to_apply": True,
                "latest_sha": latest_sha,
                "latest_short_sha": latest_sha[:8],
                "latest_message": "release",
                "behind": 1,
                "ahead": 0,
                "checked_at": "2026-06-24T00:00:00Z",
            }
        },
    )
    monkeypatch.setattr(supervisor_state, "update_state", lambda _fn: None)
    monkeypatch.setattr(control, "get_version", lambda: "6.42.0")

    def fake_git_capture(cmd):
        if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
            return (0 if target_is_ancestor else 1, "", "")
        return (0, "6.43.0\n", "")

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)


def test_passive_update_status_ignores_cache_for_current_head(monkeypatch):
    sha = "a" * 40
    _wire_status(monkeypatch, current_sha=sha, latest_sha=sha)

    status = control._managed_update_payload(fetch=False, include_tags=False)

    assert status["available"] is False
    assert status.get("from_cache") is not True
    assert status.get("latest_sha", "") == ""


def test_passive_update_status_uses_cache_for_new_target(monkeypatch):
    current_sha = "a" * 40
    latest_sha = "b" * 40
    _wire_status(monkeypatch, current_sha=current_sha, latest_sha=latest_sha)

    status = control._managed_update_payload(fetch=False, include_tags=False)

    assert status["available"] is True
    assert status["safe_to_apply"] is True
    assert status["latest_sha"] == latest_sha
    assert status["behind"] == 1
    assert status["from_cache"] is True


def test_passive_update_status_ignores_cache_when_target_is_ancestor(monkeypatch):
    current_sha = "c" * 40
    latest_sha = "b" * 40
    _wire_status(monkeypatch, current_sha=current_sha, latest_sha=latest_sha, target_is_ancestor=True)

    status = control._managed_update_payload(fetch=False, include_tags=False)

    assert status["available"] is False
    assert status.get("from_cache") is not True
    assert status.get("latest_sha", "") == ""
