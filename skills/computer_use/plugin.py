"""Computer-use extension: screenshot + basic desktop input tools.

The implementation is intentionally substrate-thin: it detects already-installed
platform tools and returns clear unsupported/capability errors instead of
downloading binaries.

Permission caveat (honest): macOS TCC state (Screen Recording / Accessibility)
is NOT probed — verifying it needs the Quartz/pyobjc preflight APIs, which this
substrate-thin skill does not pull in. ``screencapture``/``cliclick`` exit 0
even when permission is denied (capturing only the wallpaper, or dropping
synthetic input), so callers must ensure the host has granted these. The
``screenshot`` result reports physical-pixel and logical-point dimensions so a
model can map Retina coordinates correctly.
"""

from __future__ import annotations

import json
import pathlib
import platform
import shutil
import struct
import subprocess
import time
import uuid
from typing import Any


_TIMEOUT_SEC = 10


def _png_dimensions(path: pathlib.Path) -> tuple[int, int]:
    """Physical (pixel) width/height from a PNG IHDR; (0, 0) on failure."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(24)
        if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
            width, height = struct.unpack(">II", header[16:24])
            return int(width), int(height)
    except Exception:
        pass
    return 0, 0


def _macos_logical_size() -> tuple[int, int]:
    """Logical (point) DESKTOP size via AppleScript; (0, 0) on failure.

    Returns the bounding box of the whole desktop (Finder), which equals the
    main screen on a single-display Mac. cliclick consumes logical points while
    ``screencapture`` writes physical pixels, so exposing both lets a model
    recover the Retina scale. NOTE: on a MULTI-DISPLAY Mac this is the union of
    all displays, while a no-argument ``screencapture`` captures only the main
    display — the derived scale is then approximate; the result flags it.
    """
    try:
        rc, out, _ = _run([
            "osascript", "-e",
            'tell application "Finder" to get bounds of window of desktop',
        ])
        if rc == 0:
            parts = [p.strip() for p in out.replace(",", " ").split()]
            nums = [int(p) for p in parts if p.lstrip("-").isdigit()]
            if len(nums) >= 4:
                return nums[2] - nums[0], nums[3] - nums[1]
    except Exception:
        pass
    return 0, 0


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _which(name: str) -> str:
    return shutil.which(name) or ""


def _run(cmd: list[str], *, timeout: int = _TIMEOUT_SEC) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    return int(proc.returncode), proc.stdout or "", proc.stderr or ""


def _platform() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    if system == "windows":
        return "windows"
    return system or "unknown"


class _ComputerUse:
    def __init__(self, api: Any) -> None:
        self.api = api
        self.state_dir = pathlib.Path(api.get_state_dir())
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _capabilities(self) -> dict[str, Any]:
        plat = _platform()
        return {
            "platform": plat,
            "screenshot": {
                "screencapture": bool(_which("screencapture")),
                "gnome_screenshot": bool(_which("gnome-screenshot")),
                "scrot": bool(_which("scrot")),
            },
            "input": {
                "cliclick": bool(_which("cliclick")),
                "xdotool": bool(_which("xdotool")),
                "osascript": bool(_which("osascript")),
            },
            "notes": [
                "macOS requires Screen Recording for screenshot and Accessibility for input.",
                "macOS TCC permission state is NOT verified here: screencapture/cliclick exit 0 "
                "even when denied (wallpaper-only capture / dropped input). Ensure grants are in place.",
                "macOS scroll is unsupported (cliclick has no scroll-wheel command); use key paging instead.",
                "macOS click supports left and right; middle-click is unsupported via cliclick.",
                "Linux requires an active X11/desktop session for xdotool/scrot.",
                "Windows support is deferred.",
            ],
            "permission_state_verified": False,
        }

    def capabilities(self) -> str:
        return _json({"ok": True, **self._capabilities()})

    def screenshot(self, *, job_id: str = "manual") -> str:
        job_dir = self.api.skill_job_dir(job_id or uuid.uuid4().hex[:8])
        out_dir = pathlib.Path(job_dir) / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"screenshot-{int(time.time())}.png"
        plat = _platform()
        if plat == "macos" and _which("screencapture"):
            cmd = ["screencapture", "-x", str(out_path)]
        elif plat == "linux" and _which("gnome-screenshot"):
            cmd = ["gnome-screenshot", "-f", str(out_path)]
        elif plat == "linux" and _which("scrot"):
            cmd = ["scrot", str(out_path)]
        else:
            return _json({
                "ok": False,
                "error": "no supported screenshot backend found",
                "capabilities": self._capabilities(),
            })
        try:
            rc, stdout, stderr = _run(cmd)
        except Exception as exc:  # noqa: BLE001 - surface host permission errors plainly
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}", "cmd": cmd})
        if rc != 0 or not out_path.exists():
            return _json({"ok": False, "error": stderr.strip() or stdout.strip() or f"exit {rc}", "cmd": cmd})
        px_w, px_h = _png_dimensions(out_path)
        result: dict[str, Any] = {
            "ok": True, "path": str(out_path), "backend": cmd[0],
            "width_px": px_w, "height_px": px_h,
        }
        if plat == "macos":
            log_w, log_h = _macos_logical_size()
            result["logical_width"] = log_w
            result["logical_height"] = log_h
            # The screenshot is in physical pixels but click/move consume logical
            # points. On Retina, multiply screenshot-read coordinates by 1/scale
            # before clicking. scale = px_w / logical_width (≈2.0 on Retina).
            if log_w > 0 and px_w > 0:
                scale = round(px_w / log_w, 4)
                result["scale"] = scale
                # On multi-display Macs logical_* is the union of all displays
                # while screencapture grabbed only the main one, so the ratio is
                # not a clean device scale (≈1.0/2.0/3.0). Flag it as approximate.
                if abs(scale - round(scale)) > 0.02 and abs(scale * 2 - round(scale * 2)) > 0.02:
                    result["scale_approx"] = True
            result["coordinate_note"] = (
                "click/move use LOGICAL points; divide pixel coords read off this "
                "image by 'scale' before clicking. If scale_approx is set "
                "(multi-display), prefer accessibility-tree coordinates."
            )
        return _json(result)

    def click(self, *, x: int, y: int, button: str = "left", double: bool = False) -> str:
        plat = _platform()
        button = str(button or "left").strip().lower()
        # Validate the button: an unknown value (or " middle " with whitespace)
        # must NOT silently degrade to a left-click reporting ok:true.
        if button not in ("left", "right", "middle"):
            return _json({"ok": False, "error": f"unknown button {button!r} (use left/right/middle)"})
        # cliclick treats a NEGATIVE coordinate as a relative move (e.g. on a
        # multi-monitor layout), silently clicking the wrong place. Reject it.
        if int(x) < 0 or int(y) < 0:
            return _json({"ok": False, "error": f"negative coordinates not allowed ({x},{y})"})
        if plat == "macos" and _which("cliclick"):
            if button == "middle":
                return _json({"ok": False, "error": "middle-click unsupported on macOS (cliclick has no middle button)"})
            # Honour the button on macOS too: rc = right click, c/dc = left.
            op = "rc" if button == "right" else ("dc" if double else "c")
            cmd = ["cliclick", f"{op}:{int(x)},{int(y)}"]
        elif plat == "linux" and _which("xdotool"):
            button_id = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
            cmd = ["xdotool", "mousemove", str(int(x)), str(int(y)), "click", "--repeat", "2" if double else "1", button_id]
        else:
            return _json({"ok": False, "error": "no supported click backend found", "capabilities": self._capabilities()})
        return self._exec_input(cmd)

    def move(self, *, x: int, y: int) -> str:
        plat = _platform()
        if int(x) < 0 or int(y) < 0:
            return _json({"ok": False, "error": f"negative coordinates not allowed ({x},{y})"})
        if plat == "macos" and _which("cliclick"):
            cmd = ["cliclick", f"m:{int(x)},{int(y)}"]
        elif plat == "linux" and _which("xdotool"):
            cmd = ["xdotool", "mousemove", str(int(x)), str(int(y))]
        else:
            return _json({"ok": False, "error": "no supported mouse-move backend found", "capabilities": self._capabilities()})
        return self._exec_input(cmd)

    def type_text(self, *, text: str, interval_ms: int = 0) -> str:
        plat = _platform()
        text = str(text or "")
        if plat == "macos" and _which("cliclick"):
            cmd = ["cliclick", f"t:{text}"]
        elif plat == "linux" and _which("xdotool"):
            cmd = ["xdotool", "type", "--delay", str(max(0, int(interval_ms or 0))), text]
        else:
            return _json({"ok": False, "error": "no supported typing backend found", "capabilities": self._capabilities()})
        # Typing can take longer than the default 10s input cap (long text /
        # per-char delay); allow up to the registered 20s tool budget so a long
        # type is not truncated mid-input.
        est = 18
        return self._exec_input(cmd, timeout=est)

    def key(self, *, keys: str) -> str:
        combo = str(keys or "").strip()
        if not combo:
            return _json({"ok": False, "error": "keys is required"})
        plat = _platform()
        if plat == "macos" and _which("cliclick"):
            # cliclick `kp:` takes a SINGLE named key (its names CONTAIN hyphens,
            # e.g. arrow-down, page-down) — so split a combo ONLY on '+', never on
            # '-' (that would shatter "page-down" into "page"+"down"). Modifier
            # combos hold modifiers down, tap the key, release: kd:cmd t:s ku:cmd.
            parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
            _mods = {"cmd": "cmd", "command": "cmd", "ctrl": "ctrl", "control": "ctrl",
                     "alt": "alt", "option": "alt", "opt": "alt", "shift": "shift", "fn": "fn"}
            # Map common key aliases to cliclick's named keys.
            _keymap = {
                "pagedown": "page-down", "page_down": "page-down", "pgdn": "page-down",
                "pageup": "page-up", "page_up": "page-up", "pgup": "page-up",
                "down": "arrow-down", "up": "arrow-up", "left": "arrow-left", "right": "arrow-right",
                "arrowdown": "arrow-down", "arrowup": "arrow-up", "arrowleft": "arrow-left", "arrowright": "arrow-right",
                "enter": "return", "escape": "esc", "del": "delete",
            }
            if not parts:
                return _json({"ok": False, "error": "keys is required"})
            mod_tokens = parts[:-1]
            unknown = [t for t in mod_tokens if t not in _mods]
            if unknown:
                return _json({"ok": False, "error": f"unknown key modifier(s): {unknown}"})
            base = _keymap.get(parts[-1], parts[-1])
            inner = f"t:{base}" if len(base) == 1 else f"kp:{base}"
            if mod_tokens:
                mods = [_mods[t] for t in mod_tokens]
                cmd = ["cliclick", f"kd:{','.join(mods)}", inner, f"ku:{','.join(mods)}"]
            else:
                cmd = ["cliclick", inner]
        elif plat == "linux" and _which("xdotool"):
            cmd = ["xdotool", "key", combo]
        else:
            return _json({"ok": False, "error": "no supported key backend found", "capabilities": self._capabilities()})
        return self._exec_input(cmd)

    def scroll(self, *, clicks: int = 3, direction: str = "down") -> str:
        direction = str(direction or "down").lower()
        amount = max(1, min(20, abs(int(clicks or 1))))
        plat = _platform()
        if plat == "macos":
            # cliclick has NO scroll-wheel command — its `w:` is WAIT, not
            # wheel. Faking it returned ok:true while nothing scrolled. Be
            # honest: scroll is unsupported here; the model should page via the
            # `key` tool (Page_Down / arrow keys) on macOS instead.
            return _json({
                "ok": False,
                "error": "scroll is unsupported on macOS (no cliclick scroll-wheel command); "
                         "use the key tool with Page_Down/Page_Up or arrow keys instead",
                "capabilities": self._capabilities(),
            })
        if plat == "linux" and _which("xdotool"):
            # X11 scroll buttons: 4=up, 5=down, 6=left, 7=right. Validate so a
            # typo / unsupported direction does not silently scroll up.
            button = {"down": "5", "up": "4", "left": "6", "right": "7"}.get(direction)
            if button is None:
                return _json({"ok": False, "error": f"unknown scroll direction {direction!r} (use up/down/left/right)"})
            cmd = ["xdotool", "click", "--repeat", str(amount), button]
        else:
            return _json({"ok": False, "error": "no supported scroll backend found", "capabilities": self._capabilities()})
        return self._exec_input(cmd)

    def ax_tree(self) -> str:
        plat = _platform()
        if plat == "macos" and _which("osascript"):
            script = (
                'tell application "System Events" to get the name of every process '
                'whose background only is false'
            )
            try:
                rc, stdout, stderr = _run(["osascript", "-e", script])
            except Exception as exc:  # noqa: BLE001
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            if rc == 0:
                return _json({"ok": True, "platform": plat, "visible_processes": [p.strip() for p in stdout.split(",") if p.strip()]})
            return _json({"ok": False, "error": stderr.strip() or stdout.strip() or f"exit {rc}"})
        return _json({"ok": False, "error": "accessibility tree backend unsupported on this platform", "capabilities": self._capabilities()})

    def window_list(self) -> str:
        plat = _platform()
        if plat == "macos" and _which("osascript"):
            script = (
                'tell application "System Events" to get the name of every process '
                'whose background only is false'
            )
            try:
                rc, stdout, stderr = _run(["osascript", "-e", script])
            except Exception as exc:  # noqa: BLE001
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            if rc == 0:
                return _json({"ok": True, "platform": plat, "windows": [p.strip() for p in stdout.split(",") if p.strip()]})
            return _json({"ok": False, "error": stderr.strip() or stdout.strip() or f"exit {rc}"})
        if plat == "linux" and _which("wmctrl"):
            try:
                rc, stdout, stderr = _run(["wmctrl", "-l"])
            except Exception as exc:  # noqa: BLE001
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            if rc == 0:
                windows = [line.strip() for line in stdout.splitlines() if line.strip()]
                return _json({"ok": True, "platform": plat, "windows": windows})
            return _json({"ok": False, "error": stderr.strip() or stdout.strip() or f"exit {rc}"})
        return _json({"ok": False, "error": "window listing backend unsupported on this platform", "capabilities": self._capabilities()})

    def _exec_input(self, cmd: list[str], *, timeout: int = _TIMEOUT_SEC) -> str:
        try:
            rc, stdout, stderr = _run(cmd, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}", "cmd": cmd})
        if rc != 0:
            return _json({"ok": False, "error": stderr.strip() or stdout.strip() or f"exit {rc}", "cmd": cmd})
        return _json({"ok": True, "cmd": cmd})


def register(api: Any) -> None:
    impl = _ComputerUse(api)
    api.register_tool("capabilities", lambda: impl.capabilities(), description="Report available computer-use backends.", schema={"type": "object", "properties": {}}, timeout_sec=5)
    api.register_tool("screenshot", impl.screenshot, description="Capture the current desktop screenshot to skill state and return its PNG path.", schema={"type": "object", "properties": {"job_id": {"type": "string", "default": "manual"}}}, timeout_sec=15)
    api.register_tool("click", impl.click, description="Click desktop coordinates.", schema={"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string", "default": "left"}, "double": {"type": "boolean", "default": False}}, "required": ["x", "y"]}, timeout_sec=10)
    api.register_tool("move", impl.move, description="Move mouse pointer to desktop coordinates.", schema={"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["x", "y"]}, timeout_sec=10)
    api.register_tool("type_text", impl.type_text, description="Type text into the focused application.", schema={"type": "object", "properties": {"text": {"type": "string"}, "interval_ms": {"type": "integer", "default": 0}}, "required": ["text"]}, timeout_sec=20)
    api.register_tool("key", impl.key, description="Press a key or key combination such as Return, Ctrl+L, or command+s depending on backend support.", schema={"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]}, timeout_sec=10)
    api.register_tool("scroll", impl.scroll, description="Scroll the active view.", schema={"type": "object", "properties": {"clicks": {"type": "integer", "default": 3}, "direction": {"type": "string", "default": "down"}}}, timeout_sec=10)
    api.register_tool("window_list", lambda: impl.window_list(), description="List visible desktop windows/processes when a backend is available.", schema={"type": "object", "properties": {}}, timeout_sec=10)
    api.register_tool("ax_tree", lambda: impl.ax_tree(), description="Return a best-effort accessibility summary.", schema={"type": "object", "properties": {}}, timeout_sec=10)
