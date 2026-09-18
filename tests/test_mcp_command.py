"""Tests for the /mcp command: classification and status formatting.

``/mcp`` mirrors Claude Code's command — it lists configured MCP servers with
their connection status and tools, and supports reconnect/enable/disable
subcommands.  The live status comes from ``client.get_mcp_status()`` (exercised
in the running app); here we pin the pure pieces: the parser and the formatter.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from commands import classify, format_mcp_status


def test_classify_mcp():
    assert classify("/mcp") == ("mcp", "")
    assert classify("/mcp tools") == ("mcp", "tools")
    assert classify("/mcp reconnect github") == ("mcp", "reconnect github")
    assert classify("/mcp disable  sentry") == ("mcp", "disable  sentry")


def test_format_empty():
    out = format_mcp_status([])
    assert "No MCP servers configured" in out
    assert "--mcp-config" in out


SERVERS = [
    {
        "name": "github",
        "status": "connected",
        "scope": "user",
        "serverInfo": {"name": "github-mcp", "version": "1.2.0"},
        "config": {"type": "http", "url": "https://api.example/mcp"},
        "tools": [
            {"name": "create_issue", "description": "Create an issue"},
            {"name": "list_repos"},
        ],
    },
    {
        "name": "sentry",
        "status": "needs-auth",
        "scope": "project",
        "config": {"type": "sse", "url": "https://sentry/mcp"},
    },
    {
        "name": "localdb",
        "status": "failed",
        "scope": "local",
        "error": "connection refused",
        "config": {"type": "stdio", "command": "db-mcp"},
    },
]


def test_format_list():
    out = format_mcp_status(SERVERS)
    assert "3 MCP servers" in out
    # Status labels.
    assert "github — connected" in out
    assert "sentry — needs auth" in out
    assert "localdb — failed" in out
    # Handshake info, scope, transport.
    assert "github-mcp v1.2.0" in out
    assert "[user]" in out
    assert "transport: http" in out
    # Errors and auth hints.
    assert "error: connection refused" in out
    assert "run /mcp reconnect sentry to authenticate" in out
    # Tool summary (not full list in default mode).
    assert "2 tools: create_issue, list_repos" in out
    # Management footer.
    assert "/mcp reconnect <server>" in out


def test_format_tools_mode_filtered():
    out = format_mcp_status(SERVERS, tools_mode=True, filter_name="github")
    assert "1 MCP server" in out
    assert "- create_issue  Create an issue" in out
    assert "- list_repos" in out
    # Only the filtered server appears.
    assert "sentry" not in out
    assert "localdb" not in out


def test_format_unknown_filter():
    out = format_mcp_status(SERVERS, tools_mode=True, filter_name="nope")
    assert "No MCP server named 'nope'" in out


def test_format_connected_no_tools():
    out = format_mcp_status([{"name": "bare", "status": "connected"}])
    assert "bare — connected" in out
    assert "(no tools)" in out
