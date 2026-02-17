"""GitHub инструменты: issues, comments, reactions."""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gh_cmd(args: List[str], ctx: ToolContext, timeout: int = 30, input_data: Optional[str] = None) -> str:
    """Run `gh` CLI command and return stdout or error string."""
    cmd = ["gh"] + args
    try:
        res = subprocess.run(
            cmd,
            cwd=str(ctx.repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_data,
        )
        if res.returncode != 0:
            err = (res.stderr or "").strip()
            # Only return first line of stderr, truncated to 200 chars for security
            return f"⚠️ GH_ERROR: {err.split(chr(10))[0][:200]}"
        return res.stdout.strip()
    except FileNotFoundError:
        return "⚠️ GH_ERROR: `gh` CLI not found."
    except subprocess.TimeoutExpired:
        return f"⚠️ GH_TIMEOUT: exceeded {timeout}s."
    except Exception as e:
        return f"⚠️ GH_ERROR: {e}"


def _get_repo_slug(ctx: ToolContext) -> str:
    """Get 'owner/repo' from git remote."""
    try:
        res = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            cwd=str(ctx.repo_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        log.debug("Failed to get repo slug from gh", exc_info=True)
    return "razzant/ouroboros"  # fallback


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _list_issues(ctx: ToolContext, state: str = "open", labels: str = "", limit: int = 20) -> str:
    """List GitHub issues with optional filters."""
    args = [
        "issue", "list",
        "--state", state,
        "--limit", str(min(limit, 50)),
        "--json", "number,title,body,labels,createdAt,author,assignees,state",
    ]
    if labels:
        args.extend(["--label", labels])

    raw = _gh_cmd(args, ctx)
    if raw.startswith("⚠️"):
        return raw

    try:
        issues = json.loads(raw)
    except json.JSONDecodeError:
        return f"⚠️ Failed to parse issues JSON: {raw[:500]}"

    if not issues:
        return f"No {state} issues found."

    lines = [f"**{len(issues)} {state} issue(s):**\n"]
    for issue in issues:
        labels_str = ", ".join(l.get("name", "") for l in issue.get("labels", []))
        author = issue.get("author", {}).get("login", "unknown")
        lines.append(
            f"- **#{issue['number']}** {issue['title']}"
            f" (by @{author}{', labels: ' + labels_str if labels_str else ''})"
        )
        body = (issue.get("body") or "").strip()
        if body:
            # Show first 200 chars of body
            preview = body[:200] + ("..." if len(body) > 200 else "")
            lines.append(f"  > {preview}")

    return "\n".join(lines)


def _get_issue(ctx: ToolContext, number: int) -> str:
    """Get a single issue with full details and comments."""
    if number <= 0:
        return "⚠️ issue number must be positive"

    args = [
        "issue", "view", str(number),
        "--json", "number,title,body,labels,createdAt,author,assignees,state,comments",
    ]

    raw = _gh_cmd(args, ctx)
    if raw.startswith("⚠️"):
        return raw

    try:
        issue = json.loads(raw)
    except json.JSONDecodeError:
        return f"⚠️ Failed to parse issue JSON: {raw[:500]}"

    labels_str = ", ".join(l.get("name", "") for l in issue.get("labels", []))
    author = issue.get("author", {}).get("login", "unknown")

    lines = [
        f"## Issue #{issue['number']}: {issue['title']}",
        f"**State:** {issue['state']}  |  **Author:** @{author}",
    ]
    if labels_str:
        lines.append(f"**Labels:** {labels_str}")

    body = (issue.get("body") or "").strip()
    if body:
        lines.append(f"\n**Body:**\n{body[:3000]}")

    comments = issue.get("comments", [])
    if comments:
        lines.append(f"\n**Comments ({len(comments)}):**")
        for c in comments[:10]:  # limit to 10 most recent
            c_author = c.get("author", {}).get("login", "unknown")
            c_body = (c.get("body") or "").strip()[:500]
            lines.append(f"\n@{c_author}:\n{c_body}")

    return "\n".join(lines)


def _comment_on_issue(ctx: ToolContext, number: int, body: str) -> str:
    """Add a comment to an issue."""
    if number <= 0:
        return "⚠️ issue number must be positive"

    if not body or not body.strip():
        return "⚠️ Comment body cannot be empty."

    # Pass body via stdin to prevent argument injection
    args = ["issue", "comment", str(number), "--body-file", "-"]
    raw = _gh_cmd(args, ctx, input_data=body)
    if raw.startswith("⚠️"):
        return raw
    return f"✅ Comment added to issue #{number}."


def _close_issue(ctx: ToolContext, number: int, comment: str = "") -> str:
    """Close an issue with optional closing comment."""
    if number <= 0:
        return "⚠️ issue number must be positive"

    if comment and comment.strip():
        # Add comment first
        result = _comment_on_issue(ctx, number, comment)
        if result.startswith("⚠️"):
            return result

    args = ["issue", "close", str(number)]
    raw = _gh_cmd(args, ctx)
    if raw.startswith("⚠️"):
        return raw
    return f"✅ Issue #{number} closed."


def _create_issue(ctx: ToolContext, title: str, body: str = "", labels: str = "") -> str:
    """Create a new GitHub issue."""
    if not title or not title.strip():
        return "⚠️ Issue title cannot be empty."

    # Use --flag=value form to prevent argument injection
    args = ["issue", "create", f"--title={title}"]
    if body:
        # Pass body via stdin to prevent argument injection
        args.append("--body-file=-")
        raw = _gh_cmd(args, ctx, input_data=body)
    else:
        raw = _gh_cmd(args, ctx)

    if labels:
        # For existing issue, add labels separately
        if not raw.startswith("⚠️"):
            # Extract issue number from URL in raw output
            import re
            match = re.search(r'/issues/(\d+)', raw)
            if match:
                issue_num = int(match.group(1))
                label_args = ["issue", "edit", str(issue_num), f"--add-label={labels}"]
                _gh_cmd(label_args, ctx)

    if raw.startswith("⚠️"):
        return raw
    return f"✅ Issue created: {raw}"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("list_github_issues", {
            "name": "list_github_issues",
            "description": "List GitHub issues. Use to check for new tasks, bug reports, or feature requests from the creator or contributors.",
            "parameters": {"type": "object", "properties": {
                "state": {"type": "string", "default": "open", "enum": ["open", "closed", "all"], "description": "Filter by state"},
                "labels": {"type": "string", "default": "", "description": "Filter by label (comma-separated)"},
                "limit": {"type": "integer", "default": 20, "description": "Max issues to return (max 50)"},
            }, "required": []},
        }, _list_issues),

        ToolEntry("get_github_issue", {
            "name": "get_github_issue",
            "description": "Get full details of a GitHub issue including body and comments.",
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "Issue number"},
            }, "required": ["number"]},
        }, _get_issue),

        ToolEntry("comment_on_issue", {
            "name": "comment_on_issue",
            "description": "Add a comment to a GitHub issue. Use to respond to issues, share progress, or ask clarifying questions.",
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "Issue number"},
                "body": {"type": "string", "description": "Comment text (markdown)"},
            }, "required": ["number", "body"]},
        }, _comment_on_issue),

        ToolEntry("close_github_issue", {
            "name": "close_github_issue",
            "description": "Close a GitHub issue with optional closing comment.",
            "parameters": {"type": "object", "properties": {
                "number": {"type": "integer", "description": "Issue number"},
                "comment": {"type": "string", "default": "", "description": "Optional closing comment"},
            }, "required": ["number"]},
        }, _close_issue),

        ToolEntry("create_github_issue", {
            "name": "create_github_issue",
            "description": "Create a new GitHub issue. Use for tracking tasks, documenting bugs, or planning features.",
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string", "description": "Issue title"},
                "body": {"type": "string", "default": "", "description": "Issue body (markdown)"},
                "labels": {"type": "string", "default": "", "description": "Labels (comma-separated)"},
            }, "required": ["title"]},
        }, _create_issue),
    ]
