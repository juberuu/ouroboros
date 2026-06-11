from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = REPO_ROOT / "skills" / "computer_use" / "plugin.py"
SKILL_PATH = REPO_ROOT / "skills" / "computer_use" / "SKILL.md"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("computer_use_plugin", PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _API:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.tools = {}

    def get_state_dir(self) -> str:
        return str(self.state_dir)

    def skill_job_dir(self, job_id: str) -> Path:
        path = self.state_dir / "jobs" / job_id
        (path / "output").mkdir(parents=True, exist_ok=True)
        return path

    def register_tool(self, name, handler, **metadata):
        self.tools[name] = {"handler": handler, "metadata": metadata}


def test_computer_use_registers_expected_tools(tmp_path):
    module = _load_plugin()
    api = _API(tmp_path)

    module.register(api)

    assert {
        "capabilities",
        "screenshot",
        "click",
        "move",
        "type_text",
        "key",
        "scroll",
        "window_list",
        "ax_tree",
    } <= set(api.tools)


def test_computer_use_manifest_declares_subprocess_permission():
    text = SKILL_PATH.read_text(encoding="utf-8")

    assert "permissions: [tool, subprocess]" in text


def test_computer_use_screenshot_uses_detected_backend(tmp_path, monkeypatch):
    module = _load_plugin()
    api = _API(tmp_path)
    module.register(api)

    monkeypatch.setattr(module, "_platform", lambda: "linux")
    monkeypatch.setattr(module, "_which", lambda name: "/usr/bin/gnome-screenshot" if name == "gnome-screenshot" else "")

    def fake_run(cmd, **_kwargs):
        out = Path(cmd[-1])
        out.write_bytes(b"png")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = json.loads(api.tools["screenshot"]["handler"](job_id="case1"))

    assert result["ok"] is True
    assert result["backend"] == "gnome-screenshot"
    assert Path(result["path"]).read_bytes() == b"png"


def test_computer_use_reports_missing_backends(tmp_path, monkeypatch):
    module = _load_plugin()
    api = _API(tmp_path)
    module.register(api)
    monkeypatch.setattr(module, "_platform", lambda: "linux")
    monkeypatch.setattr(module, "_which", lambda _name: "")

    result = json.loads(api.tools["click"]["handler"](x=1, y=2))

    assert result["ok"] is False
    assert "no supported click backend" in result["error"]
    assert result["capabilities"]["platform"] == "linux"


def test_computer_use_window_list_uses_linux_backend(tmp_path, monkeypatch):
    module = _load_plugin()
    api = _API(tmp_path)
    module.register(api)
    monkeypatch.setattr(module, "_platform", lambda: "linux")
    monkeypatch.setattr(module, "_which", lambda name: "/usr/bin/wmctrl" if name == "wmctrl" else "")

    def fake_run(cmd, **_kwargs):
        assert cmd == ["wmctrl", "-l"]
        return SimpleNamespace(returncode=0, stdout="0x001 host Browser\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = json.loads(api.tools["window_list"]["handler"]())

    assert result == {"ok": True, "platform": "linux", "windows": ["0x001 host Browser"]}


# --- NW-5: macOS-branch coverage (previously only the linux path was tested) ---

def _macos_impl(tmp_path, monkeypatch, captured):
    module = _load_plugin()
    monkeypatch.setattr(module, "_platform", lambda: "macos")
    monkeypatch.setattr(module, "_which", lambda name: "/usr/bin/cliclick" if name == "cliclick" else "")

    def fake_run(cmd, *a, **k):
        captured.append(list(cmd))
        return module.subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return module._ComputerUse(_API(tmp_path))


def test_macos_scroll_is_honest_unsupported_not_fake_wait(tmp_path, monkeypatch):
    captured: list = []
    impl = _macos_impl(tmp_path, monkeypatch, captured)
    result = json.loads(impl.scroll(clicks=3, direction="down"))
    assert result["ok"] is False
    assert "unsupported on macOS" in result["error"]
    # Must NOT have issued a cliclick `w:` (wait) masquerading as a scroll.
    assert not any(any(str(part).startswith("w:") for part in cmd) for cmd in captured)


def test_macos_right_click_uses_rc(tmp_path, monkeypatch):
    captured: list = []
    impl = _macos_impl(tmp_path, monkeypatch, captured)
    json.loads(impl.click(x=10, y=20, button="right"))
    assert captured and captured[-1] == ["cliclick", "rc:10,20"]


def test_macos_middle_click_honest_unsupported(tmp_path, monkeypatch):
    captured: list = []
    impl = _macos_impl(tmp_path, monkeypatch, captured)
    result = json.loads(impl.click(x=10, y=20, button="middle"))
    assert result["ok"] is False and "middle" in result["error"]


def test_negative_coordinates_rejected(tmp_path, monkeypatch):
    captured: list = []
    impl = _macos_impl(tmp_path, monkeypatch, captured)
    assert json.loads(impl.click(x=-5, y=20))["ok"] is False
    assert json.loads(impl.move(x=10, y=-1))["ok"] is False
    assert captured == []  # no cliclick issued for invalid coords


def test_macos_key_combo_uses_modifier_down_up(tmp_path, monkeypatch):
    captured: list = []
    impl = _macos_impl(tmp_path, monkeypatch, captured)
    json.loads(impl.key(keys="command+s"))
    # kd:cmd t:s ku:cmd (modifier held, key tapped, modifier released).
    assert captured[-1] == ["cliclick", "kd:cmd", "t:s", "ku:cmd"]


def test_capabilities_reports_permission_state_unverified(tmp_path, monkeypatch):
    captured: list = []
    impl = _macos_impl(tmp_path, monkeypatch, captured)
    caps = json.loads(impl.capabilities())
    assert caps["permission_state_verified"] is False
