import json
from unittest.mock import MagicMock, patch


def _run(ticket, my_groups=(1,), me=7, put_status=200):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        r = MagicMock(status_code=put_status if method == "PUT" else 200)
        if url.endswith("/users/me"):
            r.json.return_value = {"user": {"id": me}}
        elif "group_memberships" in url:
            r.json.return_value = {"group_memberships": [{"group_id": g} for g in my_groups]}
        else:
            r.json.return_value = {"ticket": ticket}
        return r
    from zendesk_mcp.tools.handover import _hand_over_data
    with patch("zendesk_mcp.tools.handover.auth.request", side_effect=request), \
         patch("zendesk_mcp.tools.handover.get_oauth_session", return_value=("acme", "t")):
        return _hand_over_data(5, 2), [c for c in calls if c[0] == "PUT"]


def test_hands_over_an_unassigned_ticket_in_my_group():
    out, puts = _run({"id": 5, "group_id": 1, "assignee_id": None, "updated_at": "2026-11-23T10:00:00Z"})
    assert json.loads(out) == {"ticket_id": 5, "handed_over": True, "from_group_id": 1, "to_group_id": 2}
    sent = puts[0][2]["json"]["ticket"]
    assert sent == {"group_id": 2, "assignee_id": None, "safe_update": True, "updated_stamp": "2026-11-23T10:00:00Z"}
    assert puts[0][1].endswith("/tickets/5")


def test_hands_over_a_ticket_assigned_to_me():
    out, puts = _run({"id": 5, "group_id": 1, "assignee_id": 7})
    assert json.loads(out)["handed_over"] is True and puts


def test_already_in_the_target_group_is_a_no_op():
    out, puts = _run({"id": 5, "group_id": 2, "assignee_id": None})
    assert json.loads(out)["handed_over"] is False and not puts


def test_refuses_outside_my_groups():
    out, puts = _run({"id": 5, "group_id": 3, "assignee_id": None})
    assert out.startswith("Refused") and not puts


def test_refuses_assigned_to_someone_else():
    out, puts = _run({"id": 5, "group_id": 1, "assignee_id": 99})
    assert out.startswith("Refused") and not puts


def test_reports_a_conflict():
    out, _ = _run({"id": 5, "group_id": 1, "assignee_id": None}, put_status=409)
    assert "changed while" in out
