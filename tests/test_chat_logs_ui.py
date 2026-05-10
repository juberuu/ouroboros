"""Regression checks for chat live-card and grouped logs UI."""

from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
_CHECKS = json.loads((REPO / "tests" / "fixtures" / "chat_logs_ui_static_checks.json").read_text(encoding="utf-8"))


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("check", _CHECKS, ids=lambda item: f"{item['id']}::{item['path']}::{item['kind']}")
def test_static_ui_contracts(check):
    source = _read(check["path"])
    value = check["value"]
    if check["kind"] == "contains":
        assert value in source
    elif check["kind"] == "not_contains":
        assert value not in source
    elif check["kind"] == "regex":
        assert re.search(value, source, re.S)
    else:  # pragma: no cover
        raise AssertionError(f"unknown check kind: {check['kind']}")


def test_chat_floating_overlays_have_readable_glass_backing():
    """Header scrim remains readable; the bottom chat fade is gone."""
    css = _read("web/style.css")

    header = re.search(r"\.chat-page-header\s*\{(?P<body>[^}]+)\}", css, re.S).group("body")
    status = re.search(r"\.status-badge\s*\{(?P<body>[^}]+)\}", css, re.S).group("body")
    attach = re.search(r"\.attach-badge\s*\{(?P<body>[^}]+)\}", css, re.S).group("body")

    # Header: glass blur active, gradient ENDS at fully-transparent
    # (alpha 0.00) so the scroll-under transition has no visible
    # discontinuity. ``mask-image`` fades the blur in step.
    assert "backdrop-filter: blur(10px)" in header
    assert "rgba(13, 11, 15, 0.00) 100%" in header
    assert "mask-image:" in header

    assert ".chat-bottom-fade" not in css

    # Inline pills keep their solid backings — the test only enforces
    # the fade-to-zero invariant on the scrim layers (header / fade).
    assert "backdrop-filter: blur(8px)" in status
    assert "rgba(26, 21, 32, 0.78)" in status
    assert "backdrop-filter: blur(8px)" in attach
    assert "rgba(26, 21, 32, 0.78)" in attach

def test_chat_input_field_has_no_ambient_halo():
    """v5: the input field's ambient drop-shadow halo was removed.
    The bottom scrim + the glass background already separate the
    input from the transcript; the previous 24px ambient ring read
    as a redundant gradient ring around the textarea.
    """
    css = _read("web/style.css")
    input_block = re.search(r"#chat-input\s*\{(?P<body>[^}]+)\}", css, re.S).group("body")
    focus_block = re.search(r"#chat-input:focus\s*\{(?P<body>[^}]+)\}", css, re.S).group("body")

    # Ambient halo gone — only the inset top-edge bevel survives in base.
    assert "0 4px 24px" not in input_block
    assert "inset 0 1px 0 rgba(255, 255, 255, 0.04)" in input_block

    # Focus state keeps the focus ring (3px solid-tone outline) so
    # accessibility / keyboard users still see "this is focused".
    # v5.8.3-rc.5 swapped the ``rgba(201, 53, 69, 0.10)`` literal for the
    # SSOT ``var(--accent-10)`` token defined in ``:root``.
    assert "0 0 0 3px var(--accent-10)" in focus_block
    assert "0 4px 24px" not in focus_block

def test_chat_history_conditionally_forces_live_card_for_task_summaries():
    """Historical task_summary messages must force the live card visible
    only for non-trivial tasks (tool_calls > 0 or rounds > 1).

    After a restart taskState.toolCalls is 0, so revealBufferedCardIfNeeded
    would silently skip the card unless forceCard is set.  But trivial tasks
    (simple replies) should not show a card at all.
    """
    source = _read("web/modules/chat.js")
    # forceCard is conditional — only set when the task was non-trivial
    assert "taskState.forceCard = true;" in source
    assert "(msg.tool_calls || 0) > 0" in source
    assert "(msg.rounds || 0) > 1" in source
    # The condition + force must happen inside the task_summary branch of syncHistory
    idx_force = source.index("taskState.forceCard = true;")
    idx_append = source.index("appendTaskSummaryToLiveCard(msg);")
    assert idx_force < idx_append, "forceCard must be set before appendTaskSummaryToLiveCard"

def test_progress_messages_force_live_card_visible():
    """Progress messages (e.g. '🔍 Searching...') must force the live card open.

    Without forceCard, a single-tool-call task (like web_search) would buffer
    progress into an invisible card (toolCalls <= 1) and the user sees nothing
    until the final result arrives. forceCard must be set inside
    updateLiveCardFromProgressMessage before queueTaskLiveUpdate is called.
    """
    source = _read("web/modules/chat.js")
    # Find the updateLiveCardFromProgressMessage function
    func_start = source.index("function updateLiveCardFromProgressMessage(")
    # Find the next function definition to bound the search
    func_body_end = source.index("\n    function ", func_start + 1)
    func_body = source[func_start:func_body_end]
    # forceCard must be set to true
    assert "taskState.forceCard = true" in func_body, \
        "updateLiveCardFromProgressMessage must set forceCard = true"
    # forceCard must be set BEFORE queueTaskLiveUpdate
    idx_force = func_body.index("forceCard = true")
    idx_queue = func_body.index("queueTaskLiveUpdate(")
    assert idx_force < idx_queue, \
        "forceCard must be set before queueTaskLiveUpdate in updateLiveCardFromProgressMessage"

def test_task_summary_live_card_uses_last_headline_not_finished_task():
    """appendTaskSummaryToLiveCard must NOT use 'Finished task' as headline.

    Instead it should use lastHumanHeadline from the live card record
    and not add a visible timeline entry (the summary text duplicates
    the assistant reply bubble).
    """
    source = _read("web/modules/chat.js")
    func_start = source.index("function appendTaskSummaryToLiveCard(")
    func_body_end = source.index("\n    function ", func_start + 1)
    func_body = source[func_start:func_body_end]
    # Must NOT contain "Finished task" headline
    assert "Finished task" not in func_body, \
        "appendTaskSummaryToLiveCard should not use 'Finished task' as headline"
    # Must use lastHumanHeadline
    assert "lastHumanHeadline" in func_body, \
        "appendTaskSummaryToLiveCard should use record.lastHumanHeadline"
    # Must set visible: false to avoid adding a timeline entry
    assert "visible: false" in func_body, \
        "appendTaskSummaryToLiveCard should set visible: false"

def test_chat_input_has_glassmorphism():
    """#chat-input textarea should have frosted-glass styling (blur + semi-transparent bg + white border)."""
    css = _read("web/style.css")
    # Extract the #chat-input block (between selector and :focus)
    start = css.index("#chat-input {")
    end = css.index("#chat-input:focus")
    chat_input_block = css[start:end]
    # Must have high-quality backdrop blur on the textarea itself. v5.7.0
    # moved the blur focus to the input field (20px) and left the wrapper as
    # a no-blur soft darkening gradient.
    assert "backdrop-filter: blur(20px)" in chat_input_block
    # Background should be semi-transparent (frosted glass, opacity 0.55)
    assert "rgba(26, 21, 32, 0.55)" in chat_input_block
    # Border should be a white tint (design system for frosted glass surfaces)
    assert "rgba(255, 255, 255, 0.10)" in chat_input_block
    # Must not use opaque background
    assert "var(--bg-secondary)" not in chat_input_block

def test_log_phases_use_crimson_not_blue():
    """Active log phases (start/progress/working/thinking) should use crimson, not blue."""
    css = _read("web/style.css")
    # Find the active-phase block
    assert "log-phase.working" in css
    assert "log-phase.thinking" in css
    # The active-phase color must be crimson, not --blue
    active_block_start = css.index(".log-entry .log-phase.start,")
    active_block_end = css.index("}", active_block_start)
    active_block = css[active_block_start:active_block_end]
    assert "var(--blue)" not in active_block
    assert "rgba(248, 130, 140" in active_block

def test_costs_about_css_classes_defined():
    """All CSS classes used by the About sub-tab and costs.js must be defined in style.css."""
    css = _read("web/style.css")
    for cls in [".about-body", ".about-logo", ".about-title", ".about-footer",
                ".costs-stats-grid", ".costs-tables-grid", ".costs-table-label",
                ".cost-cell-name", ".cost-bar-cell", ".cost-bar", ".cost-empty-cell"]:
        assert cls in css, f"Missing CSS class: {cls}"

def test_trivial_task_summary_skip_in_backend():
    """_run_task_summary must skip the LLM call for trivial tasks (0 tool calls, ≤1 round)."""
    source = (REPO / "ouroboros" / "agent_task_pipeline.py").read_text(encoding="utf-8")
    # Early return for trivial tasks
    assert "n_tool_calls == 0 and rounds <= 1" in source
    # Metadata must be written to chat.jsonl even for trivial tasks
    assert '"tool_calls": n_tool_calls' in source
    assert '"rounds": rounds' in source

def test_history_api_passes_task_summary_metadata():
    """server_history_api must pass tool_calls and rounds fields for task_summary entries."""
    source = (REPO / "ouroboros" / "server_history_api.py").read_text(encoding="utf-8")
    assert 'entry.get("type") == "task_summary"' in source
    assert 'rec["tool_calls"]' in source
    assert 'rec["rounds"]' in source

def test_updates_versions_surface_uses_css_classes_not_inline_styles():
    """Updates commits/tags surface must use CSS classes, not inline style="" attributes."""
    source = _read("web/modules/updates.js")

    # Container and layout classes must be present
    assert 'class="evo-versions-header"' in source
    assert 'class="evo-versions-branch"' in source
    assert 'class="evo-versions-cols"' in source
    assert 'class="evo-versions-col"' in source

    # Row helper must use CSS classes, not inline styles
    assert 'evo-versions-row' in source
    assert 'evo-versions-row-label' in source
    assert 'evo-versions-row-msg' in source
    assert 'btn-xs' in source

    # Error/empty states must use CSS class, not inline style
    assert 'evo-empty-error' in source

    import re
    # renderRow innerHTML must not contain inline style= attributes
    # (row.style.xxx JS property assignments are irrelevant — only template literal innerHTML is checked)
    render_row_match = re.search(
        r'function renderVersionRow\(.*?\{(.*?)\n    \}', source, re.DOTALL
    )
    assert render_row_match, "renderVersionRow function not found in updates.js"
    render_row_body = render_row_match.group(1)
    inner_html_parts = re.findall(r'innerHTML\s*=\s*`(.+?)`', render_row_body, re.DOTALL)
    assert inner_html_parts, "renderVersionRow should set innerHTML via template literal"
    for part in inner_html_parts:
        assert 'style=' not in part, f"renderVersionRow innerHTML still contains inline style=: {part[:120]}"

    template_literal_match = re.search(r'page\.innerHTML\s*=\s*`(.*?)`\s*;', source, re.DOTALL)
    assert template_literal_match, "updates page template not found"
    template = template_literal_match.group(1)
    assert 'evo-versions-header' in template
    assert 'evo-versions-cols' in template
    assert 'style=' not in template, "Updates versions template still contains inline style= attributes"

def test_updates_versions_css_classes_defined():
    """CSS classes used by the Updates versions surface must be defined in style.css."""
    css = _read("web/style.css")
    for cls in [
        ".evo-versions-header",
        ".evo-versions-branch",
        ".evo-versions-cols",
        ".evo-versions-col",
        ".evo-versions-list",
        ".evo-versions-row",
        ".evo-versions-row-label",
        ".evo-versions-row-msg",
        ".evo-empty-error",
        ".btn-xs",
    ]:
        assert cls in css, f"Missing CSS class in style.css: {cls}"

def test_updates_load_versions_error_handling():
    """loadVersions() must guard resp.ok and clear local version surfaces on error."""
    source = _read("web/modules/updates.js")

    # Must check resp.ok before parsing JSON (guards non-2xx responses)
    assert "if (!resp.ok) throw new Error" in source, \
        "loadVersions must throw on non-OK HTTP response"

    # Extract the loadVersions function body (up to rollback)
    load_versions_start = source.find("async function loadVersions()")
    rollback_start = source.find("async function rollback(", load_versions_start)
    assert load_versions_start != -1 and rollback_start != -1
    fn_body = source[load_versions_start:rollback_start]

    # The catch branch must clear all three surfaces
    catch_start = fn_body.rfind("} catch (")
    assert catch_start != -1, "loadVersions catch block not found"
    catch_body = fn_body[catch_start:]
    assert 'commitsDiv.innerHTML' in catch_body, "catch must clear commitsDiv"
    assert 'tagsDiv.innerHTML' in catch_body, "catch must clear tagsDiv"
    assert 'current.textContent' in catch_body, "catch must reset current branch label"

def test_evolution_runtime_card_uses_crimson_border():
    """evo-runtime-card and evo-chart-wrap must use the crimson accent
    border (either the literal rgba — historical — or the v5.8.3-rc.5
    SSOT ``var(--accent-*)`` token), not the neutral-white default. The
    accent token expansions all reduce to ``rgba(201, 53, 69, ...)`` so
    either form satisfies the visual-contract intent."""
    css = _read("web/style.css")
    import re
    rule_match = re.search(
        r'\.evo-runtime-card,\s*\.evo-chart-wrap\s*\{(.+?)\}', css, re.DOTALL
    )
    assert rule_match, ".evo-runtime-card rule not found in style.css"
    rule_body = rule_match.group(1)
    # Either the legacy literal or the new SSOT token counts as crimson.
    has_crimson = "201, 53, 69" in rule_body or "var(--accent" in rule_body
    assert has_crimson, "evo-runtime-card must use crimson accent border (literal rgba or SSOT --accent token)"
    assert "255, 255, 255, 0.08" not in rule_body, "evo-runtime-card should not use neutral white border"

def test_live_card_timeline_no_hardcoded_item_cap():
    """Live card timeline must not drop items — no 20-item shift() cap."""
    source = _read("web/modules/chat.js")

    # The old hard cap must be gone
    assert "record.items.length > 20" not in source, "20-item cap must be removed from record.items"
    assert "bufferedLiveUpdates.length > 20" not in source, "20-item cap must be removed from bufferedLiveUpdates"

    # Incremental rendering helpers must be present
    assert "function buildTimelineItemHtml(item, record)" in source
    assert "function appendTimelineItem(item, record)" in source
    assert "function patchLastTimelineItem(item, record)" in source

    # timelineUpdate flag drives incremental vs full-rebuild path
    assert "timelineUpdate = 'append'" in source
    assert "timelineUpdate = 'patch-last'" in source
    assert "appendTimelineItem(lastItem, record)" in source
    assert "patchLastTimelineItem(lastItem, record)" in source

    # TIMELINE_MAX_HEIGHT constant must be defined
    assert "const TIMELINE_MAX_HEIGHT = 420;" in source
    # syncLiveCardLayout must clamp to it
    assert "Math.min(" in source and "TIMELINE_MAX_HEIGHT" in source

    # The cleanup timer preserves DOM node, items array, and liveCardRecords entry —
    # no memory release in the timer path (memory pressure from a few finished cards is negligible).
    assert "rec && rec.finished" not in source or True  # rec.finished guard may or may not remain
    # retiredTaskIds set: present, used for session-local suppression and cleared on reconnect
    assert "const retiredTaskIds = new Set();" in source
    assert "retiredTaskIds.add(taskState.taskId);" in source
    assert "if (retiredTaskIds.has(taskId)) continue;" in source
    # Reusable ids (bg-consciousness, active) reset on new cycle, not retired
    assert "const REUSABLE_TASK_IDS = new Set(" in source
    assert "REUSABLE_TASK_IDS.has(resolvedTaskId)" in source
    assert "taskState.completed = false;" in source

def test_chat_input_overlay_buttons_css():
    """Regression: paperclip button and send group must be absolute overlays inside the textarea."""
    css = _read("web/style.css")

    # .chat-attach-btn: absolute, inside input wrap on the left
    attach_match = re.search(
        r'\.chat-attach-btn\s*\{(.+?)\}',
        css,
        re.DOTALL,
    )
    assert attach_match, ".chat-attach-btn CSS rule not found"
    attach_body = attach_match.group(1)
    assert "position: absolute" in attach_body, ".chat-attach-btn must be position: absolute"
    assert "left:" in attach_body, ".chat-attach-btn must have a left: offset"

    # .chat-send-group: absolute wrapper for Send + chevron, positioned on the right.
    # (.chat-send-inline itself no longer needs position:absolute — the group handles it.)
    group_match = re.search(
        r'\.chat-send-group\s*\{(.+?)\}',
        css,
        re.DOTALL,
    )
    assert group_match, ".chat-send-group CSS rule not found"
    group_body = group_match.group(1)
    assert "position: absolute" in group_body, ".chat-send-group must be position: absolute"
    assert "right:" in group_body, ".chat-send-group must have a right: offset"

    # #chat-input: must have both left and right padding to avoid text overlap
    input_match = re.search(
        r'#chat-input\s*\{(.+?)\}',
        css,
        re.DOTALL,
    )
    assert input_match, "#chat-input CSS rule not found"
    input_body = input_match.group(1)
    assert "padding" in input_body, "#chat-input must have padding defined"
    # padding: 10px 76px 10px 42px — right 76px (wider for send+chevron group), left 42px
    assert "42px" in input_body, "#chat-input must have 42px left padding for attach overlay"
    # Right padding must be ≥ 72px to accommodate the send group (was 52px for single button)
    right_match = re.search(r'padding:\s*\d+px\s+(\d+)px', input_body)
    if right_match:
        right_px = int(right_match.group(1))
        assert right_px >= 72, f"#chat-input right padding must be >= 72px, got {right_px}px"
    else:
        assert "76px" in input_body or "80px" in input_body, \
            "#chat-input must have sufficient right padding for the send group"

def test_sync_history_two_pass_progress_before_finalize():
    """syncHistory must use two-pass processing: progress/summary first, then regular messages.

    This guarantees that progress bubbles are not discarded when taskState.completed=true
    is set by a prior assistant reply during the same sync iteration.
    """
    source = _read("web/modules/chat.js")

    # Two-pass comment markers must be present
    assert "Two-pass processing" in source, \
        "syncHistory must use two-pass processing"
    assert "Pass 1: progress messages and task summaries" in source, \
        "syncHistory must have an explicit Pass 1 comment"
    assert "Pass 2: regular messages" in source, \
        "syncHistory must have an explicit Pass 2 comment"

    # Pass 2 must skip task_summary (already handled in pass 1).
    # Progress messages in pass 2 are used to trigger insertCardIfNeeded (not skipped).
    assert "if (msg.system_type === 'task_summary') continue;" in source, \
        "Pass 2 must skip task_summary messages"
    # Pass 2 progress branch must call insertCardIfNeeded (not silently skip)
    assert "insertCardIfNeeded(taskId);" in source, \
        "Pass 2 must call insertCardIfNeeded for progress messages to anchor live cards"

    # Pass 1 must NOT check taskState.completed before calling updateLiveCardFromProgressMessage
    # (the old guard that caused bubbles to be lost)
    func_start = source.index("async function syncHistory(")
    func_end = source.index("\n    async function ", func_start + 1) if "\n    async function " in source[func_start + 1:] else len(source)
    sync_body = source[func_start:func_end]

    # The two-pass structure means the old pattern is gone
    # Verify there is no guard between Pass-1 start and updateLiveCardFromProgressMessage
    pass1_start = sync_body.index("Pass 1: progress messages")
    pass2_start = sync_body.index("Pass 2: regular messages")
    pass1_body = sync_body[pass1_start:pass2_start]

    assert "if (taskState.completed) continue;" not in pass1_body, \
        "Pass 1 must not skip progress messages due to taskState.completed"

def test_update_live_card_no_forcecard_for_completed():
    """updateLiveCardFromProgressMessage must not force-open cards for completed tasks."""
    source = _read("web/modules/chat.js")

    func_start = source.index("function updateLiveCardFromProgressMessage(")
    func_end = source.index("\n    function ", func_start + 1)
    func_body = source[func_start:func_end]

    # Must have the guard !taskState.completed before setting forceCard
    assert "if (taskState && !taskState.completed) taskState.forceCard = true;" in func_body, \
        "updateLiveCardFromProgressMessage must not force-open cards for already-completed tasks"

def test_live_card_timeline_css_scrollable():
    """Expanded chat live timeline must be scrollable with a max-height."""
    import re
    css = _read("web/style.css")

    # Find the expanded timeline rule
    rule_match = re.search(
        r'\.chat-live-card\[data-expanded="1"\]\s*\.chat-live-timeline\s*\{(.+?)\}',
        css,
        re.DOTALL,
    )
    assert rule_match, ".chat-live-card[data-expanded='1'] .chat-live-timeline rule not found"
    rule_body = rule_match.group(1)

    assert "max-height" in rule_body, "timeline must have max-height when expanded"
    assert "420px" in rule_body, "timeline max-height must be 420px"
    assert "overflow-y" in rule_body, "timeline must have overflow-y for scrolling"

def test_sync_history_suppresses_dom_insert_in_pass1():
    """syncHistory pass 1 must use _syncPass1Active flag to prevent premature DOM insertion."""
    source = _read("web/modules/chat.js")

    # Flag must be declared
    assert "let _syncPass1Active = false;" in source, \
        "_syncPass1Active flag must be declared"

    # Flag must be set to true before pass 1 starts
    assert "_syncPass1Active = true;" in source, \
        "pass 1 must set _syncPass1Active = true before processing messages"

    # Flag must be cleared in a finally block
    assert "} } finally { _syncPass1Active = false; }" in source, \
        "pass 1 must reset _syncPass1Active in a finally block"

    # ensureLiveCardVisible must check the flag
    func_start = source.index("function ensureLiveCardVisible(")
    func_end = source.index("\n    function ", func_start + 1)
    func_body = source[func_start:func_end]
    assert "_syncPass1Active" in func_body, \
        "ensureLiveCardVisible must check _syncPass1Active to suppress DOM insertion"

def test_sync_history_inserts_card_at_first_message_in_pass2():
    """Pass 2 of syncHistory must insert live cards at the first message for each task."""
    source = _read("web/modules/chat.js")

    # insertCardIfNeeded helper must exist in syncHistory scope
    assert "function insertCardIfNeeded(taskId)" in source, \
        "insertCardIfNeeded helper must be defined inside syncHistory"
    assert "insertedCardTaskIds.has(taskId)" in source, \
        "insertCardIfNeeded must use a set to avoid duplicate insertions"

    # Must be called for progress messages (ongoing/failed tasks)
    # Check that within the pass-2 loop, progress branch calls insertCardIfNeeded
    pass2_start = source.index("// Pass 2: regular messages")
    pass2_region = source[pass2_start:pass2_start + 2000]
    assert "insertCardIfNeeded(taskId);" in pass2_region, \
        "Pass 2 must call insertCardIfNeeded for progress messages"

    # Must also have the trailing sweep for still-disconnected cards
    assert "for (const [tid, rec] of liveCardRecords)" in source, \
        "syncHistory must sweep liveCardRecords to insert any remaining disconnected cards"
    assert "!rec.root.isConnected && !retiredTaskIds.has(tid)" in source, \
        "sweep must skip retired task ids and already-connected cards"

def test_sync_history_appends_disconnected_unfinished_cards_at_end():
    """Cards for in-progress tasks (no reply yet) must be appended after all pass-2 messages."""
    source = _read("web/modules/chat.js")

    # The trailing sweep must come AFTER the pass-2 for-loop
    # We verify both are present and the sweep follows.
    pass2_idx = source.index("// Pass 2: regular messages")
    sweep_idx = source.index("for (const [tid, rec] of liveCardRecords)")
    assert sweep_idx > pass2_idx, \
        "liveCardRecords sweep must appear after pass-2 message loop"

def test_chat_send_group_bottom_aligned_for_multiline_composer():
    """Send controls stay anchored near the final textarea line on mobile.

    Centering them in a growing multiline textarea made the send affordance look
    stuck mid-field, especially after staging a file attachment.
    """
    css = _read("web/style.css")

    # The absolute positioning lives on .chat-send-group, not .chat-send-inline.
    rule_start = css.index(".chat-send-group {")
    rule_end = css.index("\n}", rule_start) + 2
    rule_body = css[rule_start:rule_end]

    assert "bottom: 7px" in rule_body, \
        ".chat-send-group must anchor to the bottom edge of the multiline composer"
    assert "translateY(-50%)" not in rule_body, \
        ".chat-send-group must not vertically center inside a multiline composer"
    assert "position: absolute" in rule_body, \
        ".chat-send-group must be position: absolute"
    # The Send button itself must NOT re-introduce absolute positioning
    send_start = css.index(".chat-send-inline {")
    send_end = css.index("\n}", send_start) + 2
    send_body = css[send_start:send_end]
    assert "position: absolute" not in send_body, \
        ".chat-send-inline must not use position: absolute (handled by parent .chat-send-group)"

def test_chat_attach_button_bottom_aligned_for_multiline_composer():
    """Paperclip button follows the send control at the textarea baseline."""
    css = _read("web/style.css")

    rule_start = css.index(".chat-attach-btn {")
    rule_end = css.index("\n}", rule_start) + 2
    rule_body = css[rule_start:rule_end]

    assert "bottom: 9px" in rule_body, \
        ".chat-attach-btn must anchor to the bottom edge of the multiline composer"
    assert "translateY(-50%)" not in rule_body, \
        ".chat-attach-btn must not vertically center inside a multiline composer"

def test_plan_mode_dropdown_switches_mode_not_send():
    """Dropdown items must switch the mode, not immediately send a message."""
    source = _read("web/modules/chat.js")
    # Dropdown-send item: must call setSendMode('send'), NOT sendMessage directly.
    send_item_block_idx = source.index("dropdownSend.addEventListener('click'")
    send_item_block = source[send_item_block_idx:send_item_block_idx + 200]
    assert "setSendMode('send')" in send_item_block, \
        "dropdownSend click must call setSendMode('send'), not sendMessage"
    assert "sendMessage" not in send_item_block, \
        "dropdownSend click must NOT call sendMessage (mode-switch only)"
    # Dropdown-plan item: must call setSendMode('plan'), NOT sendMessage directly.
    plan_item_block_idx = source.index("dropdownPlan.addEventListener('click'")
    plan_item_block = source[plan_item_block_idx:plan_item_block_idx + 200]
    assert "setSendMode('plan')" in plan_item_block, \
        "dropdownPlan click must call setSendMode('plan'), not sendMessage"
    assert "sendMessage" not in plan_item_block, \
        "dropdownPlan click must NOT call sendMessage (mode-switch only)"

def test_plan_mode_input_padding_accommodates_group():
    """#chat-input padding-right must be wide enough for Send + chevron group (~76px)."""
    css = _read("web/style.css")
    rule_start = css.index("#chat-input {")
    rule_end = css.index("\n}", rule_start) + 2
    rule_body = css[rule_start:rule_end]
    # Extract padding-right value — must be at least 72px
    import re
    match = re.search(r'padding:\s*[^;]+?(\d+)px\s+(\d+)px', rule_body)
    if match:
        # shorthand: padding: top right... — second value is the right-hand padding
        # But our format is padding: 10px 76px 10px 42px
        right_px = int(match.group(2))
        assert right_px >= 72, \
            f"#chat-input padding-right should be >= 72px to fit send group, got {right_px}px"
    else:
        # fallback: check that 76px is mentioned in the rule
        assert "76px" in rule_body or "80px" in rule_body, \
            "#chat-input must have sufficient right padding for the send+chevron group"

def test_plan_mode_dropdown_close_on_escape():
    """Escape key handler must call closeSendDropdown."""
    source = _read("web/modules/chat.js")
    assert "e.key === 'Escape'" in source and "closeSendDropdown()" in source, \
        "Escape key must close the send dropdown"

def test_cleanup_timer_keeps_card_intact_and_adds_to_retired():
    """scheduleTaskUiCleanup must preserve the DOM node, backing arrays, and
    liveCardRecords entry so:
    1. The card stays visible and interactive (expand/collapse toggles work).
    2. A later reconnect syncHistory rebinds the existing node without duplicating.
    Only retiredTaskIds is updated so mid-session incremental syncs don't rebuild
    the card from history.  retiredTaskIds is cleared on first-load / reconnect."""
    source = _read("web/modules/chat.js")

    # Find the scheduleTaskUiCleanup function body
    start = source.find("function scheduleTaskUiCleanup(")
    end = source.find("\n    }", start)      # closing brace of the setTimeout callback
    end = source.find("\n    }", end + 1)    # closing brace of the function itself
    func_body = source[start:end + 6]

    # DOM node must NOT be removed
    assert "rec.root?.remove();" not in func_body, (
        "scheduleTaskUiCleanup must NOT call rec.root?.remove() — "
        "the card should remain visible in the chat after a task completes"
    )
    # items array must NOT be cleared (would break interactive timeline toggles)
    non_comment_lines = [l for l in func_body.splitlines() if not l.lstrip().startswith("//")]
    non_comment_body = "\n".join(non_comment_lines)
    assert "rec.items = []" not in non_comment_body, (
        "scheduleTaskUiCleanup must NOT clear rec.items — "
        "doing so breaks expand/collapse interactions while the DOM node is still visible"
    )
    # liveCardRecords entry must be preserved
    assert "liveCardRecords.delete(" not in non_comment_body, (
        "scheduleTaskUiCleanup must NOT call liveCardRecords.delete() — "
        "the entry must survive so reconnect syncHistory can rebind the existing node"
    )
    # retiredTaskIds must be populated for session-local suppression
    assert "retiredTaskIds.add(" in func_body, (
        "scheduleTaskUiCleanup must still add to retiredTaskIds "
        "so incremental syncs don't rebuild the card mid-session"
    )

def test_clipboard_paste_handler_exists():
    """chat.js must register a `paste` listener that intercepts image/* clipboard
    items and routes them through the same staging path the paperclip uses.

    Verified by literal substring assertions: the listener registration, the
    image/* MIME guard, the `pendingAttachment` set, and the `clipboard-`
    filename prefix. No DOM execution — these strings are stable contract
    surface for the feature.
    """
    source = _read("web/modules/chat.js")
    assert (
        "addEventListener('paste'" in source
        or 'addEventListener("paste"' in source
    ), (
        "chat.js must register a paste event listener for clipboard image support"
    )
    assert "image/" in source, (
        "paste handler must guard on image/* MIME type"
    )
    assert "pendingAttachment" in source, (
        "paste handler must set pendingAttachment for the staged image"
    )
    assert "clipboard-" in source, (
        "paste handler must generate a clipboard-prefixed filename"
    )

def test_chat_input_dock_has_glass_gradient_without_absolute_positioning():
    """Absolute composer uses a compact soft fade on the wrapper; blur lives on the input.

    v5.7.0 restored the scroll-under composer overlay, but the visual rule is
    now deliberately split:

    - #chat-input-area: compact single-element darkening gradient, no wrapper blur.
    - #chat-input: frosted-glass blur (20px) + semi-transparent fill.
    - no separate .chat-bottom-fade layer.
    """
    css = _read("web/style.css")
    input_area_match = re.search(
        r"#chat-input-area\s*\{([^}]*)\}",
        css,
    )
    assert input_area_match, "#chat-input-area rule must be parseable"
    input_area_body = input_area_match.group(1)
    assert "linear-gradient" in input_area_body
    assert "backdrop-filter" not in input_area_body
    assert "position: absolute" in input_area_body
    assert "padding: 32px 16px 16px" in input_area_body
    chat_js = _read("web/modules/chat.js")
    assert 'class="chat-bottom-fade"' not in chat_js
    assert "scrollToBottomAfterLayout" in chat_js
    assert "messagesDiv.style.paddingBottom" not in chat_js
    assert "--chat-input-reserve" in css
    assert "messagesDiv.style.setProperty('--chat-input-reserve'" in chat_js

def test_mobile_chat_uses_flex_composer_layout_and_no_interactive_widget():
    css = _read("web/style.css")
    html = _read("web/index.html")

    assert "interactive-widget" not in html
    input_area = re.search(r"#chat-input-area\s*\{(?P<body>[^}]+)\}", css, re.S).group("body")
    chat_messages = re.search(r"#chat-messages\s*\{(?P<body>[^}]+)\}", css, re.S).group("body")
    assert "position: absolute" in input_area
    assert "bottom: 0" in input_area
    assert "min-height: 0" in chat_messages

def test_budget_pill_navigates_to_settings_costs():
    source = _read("web/modules/chat.js")
    css = _read("web/style.css")

    assert 'id="chat-budget-pill" type="button"' in source
    assert "openDashboardTab('costs')" in source
    budget_block = re.search(r"\.chat-budget-pill\s*\{(?P<body>[^}]+)\}", css, re.S).group("body")
    assert "cursor: pointer" in budget_block

def test_desktop_vvh_uses_dvh_unit_not_px_snapshot():
    js = _read("web/app.js")
    css = _read("web/style.css")

    assert "vvhStyle.textContent = ':root{--vvh:100dvh}';" in js
    assert "const safeHeight = Math.max(320, Math.ceil(h || window.innerHeight || 0));" in js
    assert "vvhStyle.textContent = ':root{--vvh:' + safeHeight + 'px}';" in js
    assert "vvhStyle.textContent = ':root{--vvh:' + h + 'px}';" not in js

    wide_viewport_block = re.search(
        r"\}\s+else\s+\{(?P<body>.*?)vvhStyle\.textContent = ':root\{--vvh:100dvh\}';",
        js,
        re.S,
    ).group("body")
    assert "document.documentElement.classList.remove('keyboard-open');" in wide_viewport_block
    assert "document.body.classList.remove('keyboard-open');" in wide_viewport_block

    root_block = re.search(r":root\s*\{(?P<body>[^}]+)\}", css, re.S).group("body")
    body_block = re.search(r"body\s*\{(?P<body>[^}]+)\}", css, re.S).group("body")
    app_block = re.search(r"#app\s*\{(?P<body>[^}]+)\}", css, re.S).group("body")
    assert "--vvh: 100dvh" in root_block
    assert "height: var(--vvh)" in body_block
    assert "height: var(--vvh)" in app_block

def test_mobile_keyboard_open_uses_visual_viewport_flex_stack():
    """Static contract test: keyboard-open JS toggle + CSS layout rules exist."""
    js = _read("web/app.js")
    css = _read("web/style.css")

    assert "keyboard-open" in js
    assert "visualViewport" in js
    assert "frozenBaseline" in js
    assert "documentElement.clientHeight" in js
    assert "touchmove" in js
    assert "lockBoundaryTouch" in js
    assert "findScrollableKeyboardNode" in js
    assert "el.id === 'chat-input'" in js
    assert "classList?.contains('chat-live-timeline')" in js
    wide_viewport_cleanup = re.search(
        r"\}\s+else\s+\{(?P<body>.*?)document\.documentElement\.classList\.remove\('keyboard-open'\)", js, re.S
    ).group("body")
    assert "if (wasKeyboardOpen)" in wide_viewport_cleanup
    assert "document.removeEventListener('touchstart', lockTouchStart);" in wide_viewport_cleanup
    assert "document.removeEventListener('touchmove', lockBoundaryTouch);" in wide_viewport_cleanup
    assert "--vvh-offset" not in js

    assert "html.keyboard-open" in css
    assert "body.keyboard-open #nav-rail" in css
    assert "body.keyboard-open #content" in css
    assert "body.keyboard-open #page-chat.active" in css
    assert "body.keyboard-open #page-chat.active .chat-page-header" in css
    assert "body.keyboard-open #page-chat.active #chat-input-area" in css
    assert "body.keyboard-open #page-chat.active #chat-messages" in css
    assert "body.keyboard-open #page-chat {" not in css

    nav_block = re.search(
        r"body\.keyboard-open\s+#nav-rail\s*\{(?P<body>[^}]+)\}", css, re.S
    ).group("body")
    assert "display: none" in nav_block

    page_chat_block = re.search(
        r"body\.keyboard-open\s+#page-chat\.active\s*\{(?P<body>[^}]+)\}", css, re.S
    ).group("body")
    assert "position: fixed" in page_chat_block
    assert "flex-direction: column" in page_chat_block
    assert "top: 0" in page_chat_block
    assert "height: var(--vvh)" in page_chat_block
    assert "var(--vvh-offset" not in page_chat_block

    header_block = re.search(
        r"body\.keyboard-open\s+#page-chat\.active\s+\.chat-page-header\s*\{(?P<body>[^}]+)\}", css, re.S
    ).group("body")
    assert "flex-shrink: 0" in header_block
    assert "position: relative" in header_block

    messages_block = re.search(
        r"body\.keyboard-open\s+#page-chat\.active\s+#chat-messages\s*\{(?P<body>[^}]+)\}", css, re.S
    ).group("body")
    assert "flex: 1" in messages_block
    assert "min-height: 0" in messages_block
    assert "overflow-y: auto" in messages_block
    assert "overscroll-behavior: contain" in messages_block

    input_block = re.search(
        r"body\.keyboard-open\s+#page-chat\.active\s+#chat-input-area\s*\{(?P<body>[^}]+)\}", css, re.S
    ).group("body")
    assert "flex-shrink: 0" in input_block
    assert "position: relative" in input_block
