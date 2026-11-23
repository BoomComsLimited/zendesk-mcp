import json
from unittest.mock import MagicMock, patch

from zendesk_mcp.client import ConfigError

FIELDS = {"ticket_fields": [
    {"id": 1, "title": "Priority", "type": "priority", "active": True, "required": True,
     "system_field_options": [{"name": "Normal", "value": "normal"}]},
    {"id": 10, "title": "Boom Product", "type": "multiselect", "active": True, "required": True,
     "custom_field_options": [{"name": "Hosted extension", "value": "hosted_extension_"},
                              {"name": "SIP Trunk", "value": "sip_trunk"}]},
    {"id": 11, "title": "Customer impact", "type": "tagger", "active": True,
     "custom_field_options": [{"name": "Medium", "value": "medium"}]},
    {"id": 12, "title": "Reseller", "type": "text", "active": True},
    {"id": 13, "title": "Old field", "type": "text", "active": False},
], "meta": {"has_more": False}}


def _responder(ticket, put_status=200):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        r = MagicMock(status_code=put_status if method == "PUT" else 200)
        r.json.return_value = FIELDS if "ticket_fields" in url else {"ticket": ticket}
        return r
    return request, calls


def _run(fn, ticket=None, put_status=200, **kwargs):
    request, calls = _responder(ticket or {}, put_status)
    with patch("zendesk_mcp.tools.ticket_fields.auth.request", side_effect=request), \
         patch("zendesk_mcp.tools.ticket_fields.get_oauth_session", return_value=("acme", "tok")):
        return fn(**kwargs), calls


def test_get_ticket_fields_lists_active_fields_with_options():
    from zendesk_mcp.tools.ticket_fields import _ticket_fields_data
    out, calls = _run(_ticket_fields_data)
    parsed = json.loads(out)
    assert [f["title"] for f in parsed] == ["Priority", "Boom Product", "Customer impact", "Reseller"]
    assert parsed[1]["options"][0] == {"name": "Hosted extension", "value": "hosted_extension_"}
    assert "acme.zendesk.com/api/v2/ticket_fields" in calls[0][1]


def test_get_ticket_fields_follows_pages():
    from zendesk_mcp.tools.ticket_fields import _ticket_fields_data
    pages = [{"ticket_fields": [{"id": 1, "title": "A", "type": "text", "active": True}],
              "meta": {"has_more": True}, "links": {"next": "https://acme.zendesk.com/next"}},
             {"ticket_fields": [{"id": 2, "title": "B", "type": "text", "active": True}], "meta": {"has_more": False}}]

    def request(method, url, **kwargs):
        r = MagicMock(status_code=200)
        r.json.return_value = pages.pop(0)
        return r
    with patch("zendesk_mcp.tools.ticket_fields.auth.request", side_effect=request), \
         patch("zendesk_mcp.tools.ticket_fields.get_oauth_session", return_value=("acme", "tok")):
        assert [f["title"] for f in json.loads(_ticket_fields_data())] == ["A", "B"]


def test_set_fills_empty_fields_and_skips_set_ones():
    from zendesk_mcp.tools.ticket_fields import _set_ticket_fields_data
    ticket = {"id": 5, "type": None, "priority": "high", "updated_at": "2026-11-23T10:00:00Z",
              "custom_fields": [{"id": 10, "value": None}, {"id": 11, "value": "medium"}, {"id": 12, "value": ""}]}
    out, calls = _run(_set_ticket_fields_data, ticket, ticket_id=5, type="problem", priority="normal",
                      fields={"10": "hosted_extension_", "11": "medium", "12": "Elite"})
    result = json.loads(out)
    assert result["set"] == {"type": "problem", "Boom Product": ["hosted_extension_"], "Reseller": "Elite"}
    assert result["already_set"] == {"priority": "high", "Customer impact": "medium"}
    [(method, url, kwargs)] = [c for c in calls if c[0] == "PUT"]
    assert url.endswith("/tickets/5")
    sent = kwargs["json"]["ticket"]
    assert sent["type"] == "problem" and "priority" not in sent
    assert sent["custom_fields"] == [{"id": 10, "value": ["hosted_extension_"]}, {"id": 12, "value": "Elite"}]
    assert sent["safe_update"] is True and sent["updated_stamp"] == "2026-11-23T10:00:00Z"


def test_set_nothing_empty_makes_no_put():
    from zendesk_mcp.tools.ticket_fields import _set_ticket_fields_data
    ticket = {"id": 5, "priority": "high", "custom_fields": [{"id": 11, "value": "medium"}]}
    out, calls = _run(_set_ticket_fields_data, ticket, ticket_id=5, priority="low", fields={"11": "medium"})
    assert json.loads(out)["set"] == {}
    assert not [c for c in calls if c[0] == "PUT"]


def test_set_rejects_inactive_field():
    from zendesk_mcp.tools.ticket_fields import _set_ticket_fields_data
    out, calls = _run(_set_ticket_fields_data, {"id": 5, "custom_fields": []}, ticket_id=5, fields={"13": "x"})
    assert "Nothing was changed" in out and not [c for c in calls if c[0] == "PUT"]


def test_set_rejects_long_text():
    from zendesk_mcp.tools.ticket_fields import _set_ticket_fields_data
    out, calls = _run(_set_ticket_fields_data, {"id": 5, "custom_fields": []}, ticket_id=5, fields={"12": "x" * 300})
    assert "Nothing was changed" in out and not [c for c in calls if c[0] == "PUT"]


def test_set_reports_a_conflict():
    from zendesk_mcp.tools.ticket_fields import _set_ticket_fields_data
    out, _ = _run(_set_ticket_fields_data, {"id": 5, "custom_fields": [{"id": 12, "value": None}]},
                  put_status=409, ticket_id=5, fields={"12": "Elite"})
    assert "changed while" in out


def test_set_returns_config_error():
    from zendesk_mcp.tools.ticket_fields import _set_ticket_fields_data
    with patch("zendesk_mcp.tools.ticket_fields.get_oauth_session", side_effect=ConfigError("Run: zendesk-mcp setup")):
        assert "zendesk-mcp setup" in _set_ticket_fields_data(5, priority="normal")
