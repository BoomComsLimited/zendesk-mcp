import inspect
from unittest.mock import patch, MagicMock

from zendesk_mcp.client import ConfigError


@patch("zendesk_mcp.tools.write_comments.Ticket")
@patch("zendesk_mcp.tools.write_comments.Comment")
@patch("zendesk_mcp.tools.write_comments.get_client")
def test_internal_note_is_always_private(mock_get_client, mock_comment_cls, mock_ticket_cls):
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_ticket_instance = MagicMock()
    mock_ticket_cls.return_value = mock_ticket_instance

    from zendesk_mcp.tools.write_comments import _post_internal_note_data
    result = _post_internal_note_data(12345, "Internal: escalate to tier 2.")

    mock_comment_cls.assert_called_once_with(body="Internal: escalate to tier 2.", public=False)
    mock_client.tickets.update.assert_called_once_with(mock_ticket_instance)
    assert "12345" in result
    assert "internal" in result.lower()


def test_internal_note_function_has_no_public_parameter():
    # Boom fork: there must be no argument that could turn a note into a public reply.
    from zendesk_mcp.tools import write_comments
    assert "public" not in inspect.signature(write_comments._post_internal_note_data).parameters
    assert not hasattr(write_comments, "_post_comment_data")


@patch("zendesk_mcp.tools.write_comments.get_client")
def test_internal_note_returns_error_on_config_error(mock_get_client):
    mock_get_client.side_effect = ConfigError("Zendesk not configured. Run: zendesk-mcp setup")

    from zendesk_mcp.tools.write_comments import _post_internal_note_data
    assert "zendesk-mcp setup" in _post_internal_note_data(12345, "hello")


@patch("zendesk_mcp.tools.write_comments.Ticket")
@patch("zendesk_mcp.tools.write_comments.Comment")
@patch("zendesk_mcp.tools.write_comments.get_client")
def test_internal_note_returns_error_on_ticket_not_found(mock_get_client, mock_comment_cls, mock_ticket_cls):
    mock_client = MagicMock()
    mock_client.tickets.update.side_effect = Exception("RecordNotFound")
    mock_get_client.return_value = mock_client

    from zendesk_mcp.tools.write_comments import _post_internal_note_data
    assert "not found" in _post_internal_note_data(99999, "hello").lower()
