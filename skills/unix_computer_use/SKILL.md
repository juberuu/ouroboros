---
name: unix_computer_use
description: macOS/Linux desktop observation and input tools with coordinate normalization for supervised local computer-use workflows.
version: 0.2.0
type: extension
entry: plugin.py
permissions: [tool, subprocess]
env_from_settings: []
when_to_use: The user asks Ouroboros to inspect the desktop, take a screenshot, click/type/press keys, drag, or operate a local GUI under explicit human supervision.
---

# Unix Computer Use

This bundled extension exposes a reviewable computer-use substrate for macOS
and Linux (X11 + Wayland). **All actions are for supervised, low-risk local
workflows under human observation.** Windows support is deferred to a separate
skill.

Tools:

- `capabilities` reports the platform, display-session type, and detected
  screenshot/input backends.
- `screenshot` captures the desktop, downscales the image to fit WXGA
  (1280x800, configurable) and persists the exact image-to-input coordinate
  transform. Pass coordinates read off the returned image directly to the
  input tools — they are remapped automatically.
- `click` (left/right/middle, double/triple), `move`, `left_click_drag`,
  `mouse_down`/`mouse_up`, `cursor_position`, `type_text`, `key`, `hold_key`,
  `scroll`, and `wait` execute input through platform tools when available.
- `window_list` lists visible windows/processes where the platform exposes a
  lightweight backend.
- `ax_tree` returns a set-of-marks accessibility snapshot of the frontmost
  window on macOS: numbered interactive elements (role, title, center
  coordinates in input space — click them with `raw=true`). It degrades
  honestly to a process/window list when the AX walk fails or on Linux.

Coordinate contract:

- Input tools accept coordinates in the LAST screenshot's image space and
  remap them through the stored transform; `raw=true` bypasses remapping.
- macOS input consumes LOGICAL points while screenshots are physical pixels;
  the stored transform already folds the Retina scale in. On multi-display
  Macs the scale is approximate (`approx` flag) — prefer `ax_tree` marks.

Backend matrix (honest limitations):

- macOS: `screencapture` + `cliclick` + `osascript`; `sips` for downscaling.
  `scroll` is **unsupported** (cliclick has no scroll-wheel command — page via
  `key` with `page-down`/`page-up`); `middle` click and non-left
  `mouse_down`/`mouse_up` are unsupported via cliclick. `hold_key` holds
  PURE-MODIFIER combos only (`cmd`, `cmd+shift`, ...): cliclick `kd:`/`ku:`
  accept only modifiers and `kp:` cannot hold — non-modifier holds report an
  honest error. In `key` combos the base key is tapped while modifiers are
  held (`cmd+s` = hold cmd, tap s). TCC permission state (Screen Recording /
  Accessibility) is NOT verified: tools exit 0 even when denied
  (wallpaper-only capture / dropped input) — ensure grants.
- Linux X11: `xdotool` (input), `gnome-screenshot`/`scrot` (capture),
  `wmctrl` (windows), ImageMagick (downscaling). Function keys and
  case-sensitive keysyms are supported (`f5`→`F5`, `XF86AudioPlay` as-is).
- Linux Wayland: `ydotool` (pointer + typing; requires a running `ydotoold`)
  or `wtype` (typing only), `grim` or `gnome-screenshot` (capture). `xdotool`
  does NOT work on Wayland. `key` and `hold_key` are **unsupported** on
  Wayland (`ydotool key` takes raw keycodes only — combos would silently
  no-op, which this skill refuses to fake); use `type_text` for text.
  Pointer press/release uses ydotool button masks (`0x40`/`0x80`).

The skill intentionally does not hide OS permission requirements, and missing
backends produce explicit errors with the capability report instead of
guessing. The agent should prefer semantic application APIs when available and
should ask the human before destructive or sensitive UI actions.
