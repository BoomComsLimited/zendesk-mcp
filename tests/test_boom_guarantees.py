"""Boom fork: the guarantees this deployment relies on. If any of these fail, do not deploy.

The server can read tickets, add internal notes, post public replies, fill a ticket's EMPTY
triage fields, and write help-centre drafts, and nothing else that writes. A note is always private and a reply always public: no
tool takes a flag that switches between them.
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


# The only writes: an internal note or a public reply on a ticket, filling a ticket's empty
# triage fields, and NEW help-centre drafts. Nothing that changes status, assignee, requester or
# group, overwrites a field, or changes an existing article.
ALLOWED_WRITES = {"zendesk_post_internal_note", "zendesk_post_comment", "zendesk_create_article_draft",
                  "zendesk_propose_article_update", "zendesk_update_article_draft", "zendesk_set_ticket_fields"}


def test_the_only_write_tools_are_comments_and_new_article_drafts():
    assert _names(_server()) & WRITE_TOOLS == ALLOWED_WRITES
    assert ALLOWED_TOOLS & WRITE_TOOLS == ALLOWED_WRITES


@pytest.mark.parametrize("tool", ["zendesk_apply_macro", "zendesk_update_ticket",
                                  "zendesk_set_ticket_status", "zendesk_create_ticket", "zendesk_assign_ticket"])
def test_ticket_changing_tools_are_absent(tool):
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


def test_reply_sent_through_the_mcp_tool_is_public():
    srv = _server()
    client = MagicMock()
    with patch("zendesk_mcp.tools.write_comments.get_client", return_value=client):
        asyncio.run(srv.call_tool("zendesk_post_comment", {"ticket_id": 1, "body": "hello"}))
    sent = client.tickets.update.call_args.args[0]
    assert sent.comment.public is True


def test_public_replies_can_be_turned_off_by_deployment():
    assert "zendesk_post_comment" not in _names(_server(env={"ZENDESK_MCP_DISABLED_TOOLS": "zendesk_post_comment"}))


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


def _fields_and_ticket(ticket):
    """auth.request stand-in: the field definitions, then the ticket, then the PUT."""
    fields = {"ticket_fields": [
        {"id": 10, "title": "Nature of Ticket", "type": "multiselect", "active": True,
         "custom_field_options": [{"name": "Call Routing", "value": "call_routing"}]},
        {"id": 11, "title": "Customer", "type": "text", "active": True},
        {"id": 12, "title": "Don't Auto Close", "type": "checkbox", "active": True},
    ], "meta": {"has_more": False}}
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        r = MagicMock(status_code=200)
        r.json.return_value = fields if "ticket_fields" in url else {"ticket": ticket}
        return r
    return request, calls


def _set(args, ticket):
    request, calls = _fields_and_ticket(ticket)
    with patch("zendesk_mcp.tools.ticket_fields.auth.request", side_effect=request), \
         patch("zendesk_mcp.tools.ticket_fields.get_oauth_session", return_value=("acme", "t")):
        asyncio.run(_server().call_tool("zendesk_set_ticket_fields", args))
    return [c for c in calls if c[0] == "PUT"]


def test_set_ticket_fields_sends_only_triage_fields():
    ticket = {"id": 1, "updated_at": "x", "custom_fields": [{"id": 10, "value": None}, {"id": 11, "value": None}]}
    [(_, _, kwargs)] = _set({"ticket_id": 1, "priority": "normal", "fields": {"10": ["call_routing"], "11": "Acme"}},
                            ticket)
    sent = kwargs["json"]["ticket"]
    assert set(sent) <= {"type", "priority", "custom_fields", "safe_update", "updated_stamp"}
    assert sent["safe_update"] is True


def test_set_ticket_fields_never_overwrites():
    ticket = {"id": 1, "priority": "high", "updated_at": "x",
              "custom_fields": [{"id": 10, "value": ["call_routing"]}, {"id": 11, "value": "Agent's choice"}]}
    assert _set({"ticket_id": 1, "priority": "low", "fields": {"11": "Neo's guess"}}, ticket) == []


@pytest.mark.parametrize("args", [
    {"ticket_id": 1, "fields": {"12": True}},             # a checkbox: not settable
    {"ticket_id": 1, "fields": {"10": ["not_an_option"]}},  # not one of the options
    {"ticket_id": 1, "fields": {"99": "x"}},               # not a field
    {"ticket_id": 1, "fields": {"11": "two\nlines"}},       # text must be one line
    {"ticket_id": 1, "type": "spam"},
])
def test_set_ticket_fields_refuses_and_changes_nothing(args):
    ticket = {"id": 1, "updated_at": "x", "custom_fields": [{"id": 10, "value": None}, {"id": 11, "value": None}]}
    assert _set(args, ticket) == []


def test_set_ticket_fields_has_no_status_assignee_or_group_parameter():
    [tool] = [t for t in asyncio.run(_server().list_tools()) if t.name == "zendesk_set_ticket_fields"]
    assert set((getattr(tool, "input_schema", None) or tool.inputSchema)["properties"]) == {"ticket_id", "type", "priority", "fields"}
