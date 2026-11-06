import json
from zendesk_mcp.client import get_client, ConfigError
from zendesk_mcp.auth import api_error_message, TokenExpiredError
from zendesk_mcp.tools.channel import ticket_channel


def _search_tickets_data(keywords: str | None, status: str | None, limit: int) -> str:
    try:
        client = get_client()
        query = "type:ticket"
        if keywords:
            query += f" {keywords}"
        if status:
            query += f" status:{status}"
        results = client.search(query=query, sort_by="created_at", sort_order="desc")
        tickets = []
        for ticket in results:
            if len(tickets) >= limit:
                break
            tickets.append({
                "id": ticket.id,
                "subject": ticket.subject,
                "status": ticket.status,
                "priority": ticket.priority,
                "requester": {
                    "name": ticket.requester.name,
                    "email": ticket.requester.email,
                },
                "assignee": {
                    "name": ticket.assignee.name,
                    "email": ticket.assignee.email,
                } if ticket.assignee else None,
                "channel": ticket_channel(ticket),
                "created_at": str(ticket.created_at),
                "updated_at": str(ticket.updated_at),
                "description": ticket.description[:300] if ticket.description else "",
            })
        return json.dumps(tickets, indent=2)
    except (ConfigError, TokenExpiredError) as e:
        return str(e)
    except Exception as e:
        return api_error_message(e)


def _plain(value):
    """A JSON-safe copy of a zenpy attribute (str, number, bool, list of those), else None."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, (str, int, float))]
    if hasattr(value, "isoformat"):
        return str(value)
    return None


def _requester_data(ticket) -> dict:
    """Boom fork: also the requester's id, tags and account age, and their organisation, so a
    caller can tell a long-standing customer from a brand-new sender."""
    r = ticket.requester
    org = getattr(ticket, "organization", None)
    return {
        "id": _plain(getattr(r, "id", None)),
        "name": r.name,
        "email": r.email,
        "role": _plain(getattr(r, "role", None)),
        "tags": _plain(getattr(r, "tags", None)) or [],
        "created_at": _plain(getattr(r, "created_at", None)),
        "organization": {
            "id": _plain(getattr(org, "id", None)),
            "name": _plain(getattr(org, "name", None)),
            "tags": _plain(getattr(org, "tags", None)) or [],
        } if org is not None else None,
    }


def _get_ticket_data(ticket_id: int) -> str:
    try:
        client = get_client()
        ticket = client.tickets(id=ticket_id)
        return json.dumps({
            "id": ticket.id,
            "subject": ticket.subject,
            "status": ticket.status,
            "priority": ticket.priority,
            "type": ticket.type,
            "requester": _requester_data(ticket),
            "assignee": {
                "name": ticket.assignee.name,
                "email": ticket.assignee.email,
            } if ticket.assignee else None,
            "group": ticket.group.name if ticket.group else None,
            "tags": ticket.tags,
            "channel": ticket_channel(ticket),
            "created_at": str(ticket.created_at),
            "updated_at": str(ticket.updated_at),
            "description": ticket.description,
            "custom_fields": list(ticket.custom_fields or []),
            "ticket_url": f"https://{_get_subdomain()}.zendesk.com/agent/tickets/{ticket.id}",
        }, indent=2)
    except (ConfigError, TokenExpiredError) as e:
        return str(e)
    except Exception as e:
        if "RecordNotFound" in str(e) or "404" in str(e):
            return f"Ticket #{ticket_id} not found or not accessible with current credentials."
        return api_error_message(e)


def _get_subdomain() -> str:
    from zendesk_mcp.request_token import current_subdomain
    return current_subdomain()


def register_ticket_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_get_ticket(ticket_id: int) -> str:
        """Get a Zendesk ticket by ID. Returns ticket fields including status, priority, requester, assignee, tags, and description."""
        return _get_ticket_data(ticket_id)

    @mcp.tool()
    def zendesk_search_tickets(keywords: str = "", status: str = "", limit: int = 50) -> str:
        """Search Zendesk tickets by keyword and/or status. keywords: free-text search (e.g. 'login failure LDAP'). status: new, open, pending, hold, solved, closed — leave empty for all statuses. Returns id, subject, status, requester, assignee, and dates."""
        return _search_tickets_data(keywords or None, status or None, limit)
