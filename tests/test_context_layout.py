"""Unit tests for the context_layout doc-layout SSOT (low/max)."""

from ouroboros import context_layout as cl


def test_tier0_protected_core_declared():
    """The protected always-full core is a data invariant; future context-mode
    work must not silently demote any of these (BIBLE P1 / P4)."""
    expected = {
        "system",
        "bible",
        "identity",
        "scratchpad",
        "knowledge_index",
        "recent_dialogue",
    }
    assert expected <= set(cl.TIER0_ALWAYS_FULL)


def test_nav_map_lists_headings_with_line_ranges_and_omits_body():
    text = (
        "# Title\n\nintro\n\n## Alpha\n\nbody-alpha BODYSENT\n\n"
        "### Sub one\n\nx\n\n## Beta\n\nbody-beta\n"
    )
    m = cl.generate_doc_nav_map(text, title="ARCHITECTURE.md", rel_path="docs/ARCHITECTURE.md")
    assert "navigation map" in m
    assert "read_file" in m  # tells the agent how to pull full sections
    assert 'root="system_repo"' in m
    assert "Alpha" in m and "Beta" in m and "Sub one" in m
    assert "lines" in m
    # Structure only — the section bodies are NOT inlined.
    assert "BODYSENT" not in m
    assert "body-beta" not in m


def test_nav_map_is_fence_aware():
    """A '## ' line inside a code fence must not be parsed as a heading."""
    text = "## Real\n\n```\n## fake-heading-in-fence\n```\n\n## Real2\n"
    m = cl.generate_doc_nav_map(text, title="X", rel_path="x.md")
    assert "Real" in m and "Real2" in m
    assert "fake-heading-in-fence" not in m
