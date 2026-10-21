"""Boom fork: the signed-in person's Zendesk token for the current request, if any.

When the server runs per-user over HTTP (``zendesk-mcp serve``), the SDK's bearer middleware
puts the caller's verified token in a context variable. Every Zendesk call must use that token,
never the config file, so the call is made as that person.
"""


def request_token() -> tuple[str, str] | None:
    """(subdomain, token) for the caller of the current HTTP request, else None (stdio mode)."""
    from mcp.server.auth.middleware.auth_context import get_access_token

    access = get_access_token()
    if access is None:
        return None
    from zendesk_mcp.per_user import subdomain

    return subdomain(), access.token


def current_subdomain() -> str:
    """The Zendesk subdomain for this call: the signed-in request's in server mode (where there is
    no config file), otherwise the config file's."""
    per_user = request_token()
    if per_user:
        return per_user[0]
    from zendesk_mcp.config import load_config
    return (load_config().get("subdomain") or "").strip()
