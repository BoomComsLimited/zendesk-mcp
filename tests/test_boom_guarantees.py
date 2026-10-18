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


# The only writes: an internal note on a ticket, and NEW help-centre drafts. Nothing that a
# customer can see, nothing that changes a ticket or an existing article.
ALLOWED_WRITES = {"zendesk_post_internal_note", "zendesk_create_article_draft", "zendesk_propose_article_update",
                  "zendesk_update_article_draft"}


def test_the_only_write_tools_are_internal_notes_and_new_article_drafts():
    assert _names(_server()) & WRITE_TOOLS == ALLOWED_WRITES
    assert ALLOWED_TOOLS & WRITE_TOOLS == ALLOWED_WRITES


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


# --- help centre: drafts only, existing articles never touched ---------------------------------

def _record_requests(monkeypatch, article=None):
    """Capture every Zendesk call the help-centre tools make, answering with canned data."""
    calls = []

    class Resp:
        def __init__(self, data): self._d = data
        def raise_for_status(self): pass
        def json(self): return self._d

    def fake(method, url, **kw):
        calls.append((method, url, kw.get("json")))
        if "permission_groups" in url:
            return Resp({"permission_groups": [{"id": 5, "built_in": False, "edit": True}]})
        if method == "GET":
            return Resp({"article": article or {"id": 9, "title": "Orig", "section_id": 3, "html_url": "https://x/9"}})
        return Resp({"article": {"id": 99, "html_url": "https://x/99"}})

    monkeypatch.setattr("zendesk_mcp.tools.help_center.auth.request", fake)
    monkeypatch.setattr("zendesk_mcp.tools.help_center.get_oauth_session", lambda: ("example", "tok"))
    return calls


def test_a_new_article_is_always_a_draft(monkeypatch):
    calls = _record_requests(monkeypatch)
    asyncio.run(_server().call_tool("zendesk_create_article_draft", {"section_id": 3, "title": "T", "body_html": "<p>b</p>"}))
    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 1 and posts[0][2]["article"]["draft"] is True


def test_a_proposed_update_creates_a_new_draft_and_never_writes_the_original(monkeypatch):
    calls = _record_requests(monkeypatch)
    asyncio.run(_server().call_tool("zendesk_propose_article_update", {"article_id": 9, "body_html": "<p>new</p>"}))
    writes = [c for c in calls if c[0] != "GET"]
    assert len(writes) == 1
    method, url, body = writes[0]
    assert method == "POST" and url.endswith("/sections/3/articles.json")
    assert body["article"]["draft"] is True
    assert body["article"]["title"].startswith("PROPOSED UPDATE: ")
    assert "/articles/9" not in url


def test_help_centre_code_has_no_publish_or_delete_calls():
    import inspect
    from zendesk_mcp.tools import help_center
    src = inspect.getsource(help_center)
    for verb in ('"PATCH"', '"DELETE"', "'PATCH'", "'DELETE'"):
        assert verb not in src, f"help_center must not call {verb}"
    assert src.count('"PUT"') == 1, "the only PUT is the draft-only update"
    assert "draft\": False" not in src and '"draft": False' not in src


def _translation_requests(monkeypatch, draft):
    calls = []

    class Resp:
        def __init__(self, data): self._d = data
        def raise_for_status(self): pass
        def json(self): return self._d

    def fake(method, url, **kw):
        calls.append((method, url, kw.get("json")))
        return Resp({"translation": {"draft": draft}})

    monkeypatch.setattr("zendesk_mcp.tools.help_center.auth.request", fake)
    monkeypatch.setattr("zendesk_mcp.tools.help_center.get_oauth_session", lambda: ("example", "tok"))
    return calls


def test_updating_a_published_article_is_refused_and_writes_nothing(monkeypatch):
    calls = _translation_requests(monkeypatch, draft=False)
    asyncio.run(_server().call_tool("zendesk_update_article_draft", {"article_id": 9, "body_html": "<p>x</p>"}))
    assert [c for c in calls if c[0] != "GET"] == []


def test_updating_a_draft_keeps_it_a_draft(monkeypatch):
    calls = _translation_requests(monkeypatch, draft=True)
    asyncio.run(_server().call_tool("zendesk_update_article_draft", {"article_id": 9, "body_html": "<p>x</p>"}))
    puts = [c for c in calls if c[0] == "PUT"]
    assert len(puts) == 1 and puts[0][2]["translation"]["draft"] is True
