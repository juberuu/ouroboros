---
name: computer_use
description: Cross-platform desktop observation and input tools for low-risk local computer-use workflows.
version: 0.1.0
type: extension
entry: plugin.py
permissions: [tool, subprocess]
env_from_settings: []
when_to_use: The user asks Ouroboros to inspect the desktop, take a screenshot, click/type/press keys, or operate a local GUI under explicit human supervision.
---

# Computer Use

This bundled extension exposes a small, reviewable computer-use substrate:

- `capabilities` reports the detected platform and available screenshot/input
  backends.
- `screenshot` captures the current desktop into this skill's private state and
  returns the saved PNG path.
- `click`, `type_text`, `key`, `move`, and `scroll` execute basic mouse/keyboard
  actions through platform tools when available.
- `window_list` lists visible windows/processes where the platform exposes a
  lightweight backend.
- `ax_tree` returns a best-effort accessibility summary (process names), not a
  full accessibility tree; unsupported platforms return a clear `unsupported`
  response instead of guessing.

macOS specifics (honest limitations):
- `scroll` is **unsupported** on macOS — `cliclick` has no scroll-wheel command.
  Page with the `key` tool (`Page_Down`/`Page_Up`/arrow keys) instead.
- `click` supports `left` and `right` (`right` issues a real right-click);
  `middle` is unsupported via `cliclick`.
- `key` accepts modifier combos (e.g. `command+s`, `ctrl+l`); they are issued as
  modifier-down → key → modifier-up.
- `screenshot` returns physical-pixel (`width_px`/`height_px`) AND logical-point
  (`logical_width`/`logical_height`) sizes plus `scale`. Clicks/moves use LOGICAL
  points, so divide coordinates read off the (physical, Retina) screenshot by
  `scale` before clicking.
- Permission state is NOT verified: `screencapture`/`cliclick` exit 0 even when
  Screen Recording / Accessibility is denied (wallpaper-only capture / dropped
  input). The skill cannot detect this without Quartz APIs — ensure grants.
- Negative coordinates are rejected (cliclick would treat them as relative).

The skill intentionally does not hide OS permission requirements. macOS needs
Screen Recording for screenshots and Accessibility for input/AX operations.
Linux desktop control needs a graphical session plus tools such as
`gnome-screenshot` or `scrot`, `wmctrl` for window listing, and `xdotool` for
input. Windows support is deferred.

Use this for low-risk local workflows only. The agent should prefer semantic
application APIs when available and should ask the human before destructive or
sensitive UI actions.
