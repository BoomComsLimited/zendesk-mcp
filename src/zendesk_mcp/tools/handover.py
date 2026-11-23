"""Boom fork: hand a ticket over from the signed-in agent's group to another group.

``zendesk_hand_over_ticket`` is for Neo, the only member of Support - 1st Line: after his first
triage he passes each ticket to Support - 2nd Line. It is deliberately narrow:
- it only moves a ticket that is currently in a group the signed-in agent is a member of, and
  that is unassigned or assigned to the signed-in agent: it can't take a ticket away from
  another group or another agent;
- it changes the group and clears the assignee, nothing else (no status, priority, fields,
  tags or comments);
- the update is a safe update (Zendesk rejects it if the ticket changed since it was read).
"""
import json

from zendesk_mcp import auth
from zendesk_mcp.auth import api_error_message, TokenExpiredError
from zendesk_mcp.client import get_oauth_session, ConfigError


def _get(url: str) -> dict:
    response = auth.request("GET", url, timeout=30)
    response.raise_for_status()
    return response.json()


def _hand_over_data(ticket_id: int, to_group_id: int) -> str:
    try:
        subdomain, _ = get_oauth_session()
        base = f"https://{subdomain}.zendesk.com/api/v2"
        me = _get(f"{base}/users/me").get("user", {})
        my_groups = {m.get("group_id") for m in
                     _get(f"{base}/users/{me.get('id')}/group_memberships").get("group_memberships", [])}
        response = auth.request("GET", f"{base}/tickets/{int(ticket_id)}", timeout=30)
        if response.status_code == 404:
            return f"Ticket #{ticket_id} not found or not accessible with current credentials."
        response.raise_for_status()
        ticket = response.json().get("ticket", {})
        group, assignee = ticket.get("group_id"), ticket.get("assignee_id")
        if group == int(to_group_id):
            return json.dumps({"ticket_id": int(ticket_id), "handed_over": False,
                               "reason": "already in that group"})
        if group not in my_groups:
            return (f"Refused: ticket #{ticket_id} isn't in one of your groups (it's in group {group}). "
                    "Nothing was changed.")
        if assignee not in (None, me.get("id")):
            return (f"Refused: ticket #{ticket_id} is assigned to another agent ({assignee}). "
                    "Nothing was changed.")
        update = {"group_id": int(to_group_id), "assignee_id": None,
                  "safe_update": True, "updated_stamp": ticket.get("updated_at")}
        r = auth.request("PUT", f"{base}/tickets/{int(ticket_id)}", json={"ticket": update}, timeout=30)
        if r.status_code == 409:
            return f"Ticket #{ticket_id} changed while it was being handed over. Nothing was changed; try again."
        r.raise_for_status()
        return json.dumps({"ticket_id": int(ticket_id), "handed_over": True, "from_group_id": group,
                           "to_group_id": int(to_group_id)})
    except (ConfigError, TokenExpiredError) as e:
        return str(e)
    except Exception as e:
        return api_error_message(e)


def register_handover_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_hand_over_ticket(ticket_id: int, to_group_id: int) -> str:
        """Hand a ticket over to another group (to_group_id from zendesk_get_groups): sets the group and clears the assignee, nothing else. Only for a ticket that's in one of your own groups and is unassigned or assigned to you; anything else is refused and nothing changes."""
        return _hand_over_data(ticket_id, to_group_id)
