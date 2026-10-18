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


def register_write_comment_tools(mcp) -> None:
    # Boom fork: the public-reply tool (zendesk_post_comment) has been removed. This server
    # drafts for a human: it can only add notes that agents see and the requester never does.
    @mcp.tool()
    def zendesk_post_internal_note(ticket_id: int, body: str) -> str:
        """Post an internal note on a Zendesk ticket. Internal notes are only visible to agents and are never sent to the requester. Use this to draft a suggested reply for an agent to review and send."""
        return _post_internal_note_data(ticket_id, body)
