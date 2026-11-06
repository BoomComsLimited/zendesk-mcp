from zenpy.lib.api_objects import Comment, Ticket

from zendesk_mcp.client import get_client, ConfigError
from zendesk_mcp.auth import api_error_message, TokenExpiredError


def _post_internal_note_data(ticket_id: int, body: str) -> str:
    """Boom fork: always an internal note. There is deliberately no parameter for public."""
    try:
        client = get_client()
        ticket = Ticket(id=ticket_id)
        ticket.comment = Comment(body=body, public=False)
        client.tickets.update(ticket)
        return f"Internal note posted successfully on ticket #{ticket_id}."
    except (ConfigError, TokenExpiredError) as e:
        return str(e)
    except Exception as e:
        if "RecordNotFound" in str(e) or "404" in str(e):
            return f"Ticket #{ticket_id} not found or not accessible with current credentials."
        return api_error_message(e)


def _post_public_reply_data(ticket_id: int, body: str) -> str:
    """Boom fork: always a public reply. Separate from the note so neither takes a public flag."""
    try:
        client = get_client()
        ticket = Ticket(id=ticket_id)
        ticket.comment = Comment(body=body, public=True)
        client.tickets.update(ticket)
        return f"Public reply posted successfully on ticket #{ticket_id}."
    except (ConfigError, TokenExpiredError) as e:
        return str(e)
    except Exception as e:
        if "RecordNotFound" in str(e) or "404" in str(e):
            return f"Ticket #{ticket_id} not found or not accessible with current credentials."
        return api_error_message(e)


def register_write_comment_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_post_comment(ticket_id: int, body: str) -> str:
        """Post a public reply on a Zendesk ticket. The reply is visible to the requester and emailed to them. Use for customer-facing responses; use zendesk_post_internal_note for anything an agent should review first."""
        return _post_public_reply_data(ticket_id, body)

    @mcp.tool()
    def zendesk_post_internal_note(ticket_id: int, body: str) -> str:
        """Post an internal note on a Zendesk ticket. Internal notes are only visible to agents and are never sent to the requester. Use this to draft a suggested reply for an agent to review and send."""
        return _post_internal_note_data(ticket_id, body)
