"""Boom fork: the guarantees this deployment relies on. If any of these fail, do not deploy.

The server must be able to read tickets and add internal notes, and nothing else that writes.
In particular it must never be able to send anything a customer can see.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from zendesk_mcp.tool_policy import ALLOWED_TOOLS, WRITE_TOOLS


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ZENDESK_MCP_READ_ONLY", raising=False)
    monkeypatch.delenv("ZENDESK_MCP_DISABLED_TOOLS", raising=False)
    monkeypatch.setattr("zendesk_mcp.config.Path.home", lambda: tmp_path)


def _server(cfg=None, env=None):
    from zendesk_mcp.server import FastMCP, register_all

    srv = FastMCP("zendesk-mcp-boom-test")
    with patch("zendesk_mcp.tool_policy.load_config", return_value=cfg or {}), \
         patch.dict("os.environ", env or {}, clear=False):
        register_all(srv)
    return srv


def _names(srv):
    return {t.name for t in asyncio.run(srv.list_tools())}


def test_only_allowlisted_tools_are_registered():
    names = _names(_server())
    assert names <= ALLOWED_TOOLS, f"not allowlisted: {sorted(names - ALLOWED_TOOLS)}"


def test_the_only_write_tool_is_the_internal_note():
    assert _names(_server()) & WRITE_TOOLS == {"zendesk_post_internal_note"}
    assert ALLOWED_TOOLS & WRITE_TOOLS == {"zendesk_post_internal_note"}


@pytest.mark.parametrize("tool", ["zendesk_post_comment", "zendesk_apply_macro", "zendesk_update_ticket",
                                  "zendesk_set_ticket_status", "zendesk_create_ticket", "zendesk_assign_ticket"])
def test_customer_visible_or_ticket_changing_tools_are_absent(tool):
    assert tool not in _names(_server())


def test_config_and_env_cannot_widen_the_allowlist():
    # Neither "read_only: false" nor an empty disabled list can bring a tool back.
    names = _names(_server(cfg={"read_only": False, "disabled_tools": []},
                           env={"ZENDESK_MCP_READ_ONLY": "false", "ZENDESK_MCP_DISABLED_TOOLS": ""}))
    assert names <= ALLOWED_TOOLS


def test_note_sent_through_the_mcp_tool_is_private():
    srv = _server()
    client = MagicMock()
    with patch("zendesk_mcp.tools.write_comments.get_client", return_value=client):
        asyncio.run(srv.call_tool("zendesk_post_internal_note", {"ticket_id": 1, "body": "draft"}))
    sent = client.tickets.update.call_args.args[0]
    assert sent.comment.public is False
