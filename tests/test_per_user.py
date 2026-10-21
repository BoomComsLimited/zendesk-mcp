"""Boom fork: per-user mode. Every Zendesk call must be made with the caller's own token,
only agents and admins get in, and nothing falls back to the shared config file."""
import asyncio
from unittest.mock import MagicMock, patch

import httpx
import pytest

from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from zendesk_mcp import per_user
from zendesk_mcp.per_user import ZendeskTokenVerifier, admit, authorization_server_metadata

AGENT = {"id": 7, "role": "agent", "active": True, "suspended": False, "email": "sam@boom.limited"}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "example")
    monkeypatch.setenv("ZENDESK_MCP_BASE_URL", "https://zendesk.mcp.test")
    monkeypatch.delenv("ZENDESK_MCP_ALLOWED_EMAIL_DOMAINS", raising=False)
    monkeypatch.setattr("zendesk_mcp.config.Path.home", lambda: tmp_path)  # no real config file


def _as_user(token: str):
    return auth_context_var.set(AuthenticatedUser(AccessToken(token=token, client_id="zendesk", scopes=[])))


# --- who gets in -------------------------------------------------------------------------------

def test_agents_and_admins_are_admitted():
    assert admit(AGENT, frozenset()) is None
    assert admit({**AGENT, "role": "admin"}, frozenset()) is None


@pytest.mark.parametrize("user", [
    {**AGENT, "role": "end-user"},       # a customer with a Zendesk login
    {**AGENT, "active": False},
    {**AGENT, "suspended": True},
    {},
])
def test_customers_and_inactive_users_are_refused(user):
    assert admit(user, frozenset()) is not None


def test_email_domain_restriction():
    assert admit(AGENT, frozenset({"boom.limited"})) is None
    assert admit({**AGENT, "email": "x@gmail.com"}, frozenset({"boom.limited"})) is not None


def _verifier(user: dict, status: int = 200, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.headers["Authorization"])
        assert request.url.path == "/api/v2/users/me.json"
        return httpx.Response(status, json={"user": user})
    return ZendeskTokenVerifier(sub="example", domains=frozenset(), transport=httpx.MockTransport(handler))


def test_verifier_accepts_an_agent_token_and_records_who():
    access = asyncio.run(_verifier(AGENT).verify_token("tok-agent"))
    assert access is not None and access.token == "tok-agent" and access.subject == "7"


def test_verifier_refuses_a_customer_and_a_rejected_token():
    assert asyncio.run(_verifier({**AGENT, "role": "end-user"}).verify_token("t")) is None
    assert asyncio.run(_verifier({}, status=401).verify_token("t")) is None


def test_verifier_caches_so_zendesk_is_asked_once():
    calls: list = []
    v = _verifier(AGENT, calls=calls)
    asyncio.run(v.verify_token("tok"))
    asyncio.run(v.verify_token("tok"))
    assert calls == ["Bearer tok"]


# --- every call uses the caller's token ---------------------------------------------------------

def test_client_uses_the_callers_token_not_the_config_file():
    from zendesk_mcp import client
    reset = _as_user("tok-sam")
    try:
        with patch("zendesk_mcp.client.Zenpy") as zenpy, patch("zendesk_mcp.client.valid_token") as cfg:
            client.get_client()
        zenpy.assert_called_once_with(subdomain="example", oauth_token="tok-sam")
        cfg.assert_not_called()
    finally:
        auth_context_var.reset(reset)


def test_direct_requests_use_the_callers_token():
    from zendesk_mcp import auth
    reset = _as_user("tok-alex")
    try:
        with patch("zendesk_mcp.auth.httpx.request", return_value=httpx.Response(200, json={})) as req, \
             patch("zendesk_mcp.auth.valid_token") as cfg:
            auth.request("GET", "https://example.zendesk.com/api/v2/groups.json")
        assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer tok-alex"
        cfg.assert_not_called()
    finally:
        auth_context_var.reset(reset)


def test_concurrent_callers_never_see_each_others_token():
    from zendesk_mcp.request_token import request_token

    async def as_user(token):
        auth_context_var.set(AuthenticatedUser(AccessToken(token=token, client_id="zendesk", scopes=[])))
        await asyncio.sleep(0.01)
        return request_token()[1]

    async def both():
        return await asyncio.gather(as_user("tok-a"), as_user("tok-b"))

    assert asyncio.run(both()) == ["tok-a", "tok-b"]


def test_without_a_caller_the_config_file_path_is_unchanged():
    from zendesk_mcp.request_token import request_token
    assert request_token() is None


# --- discovery and the HTTP gate ---------------------------------------------------------------

def test_authorization_server_metadata_points_at_zendesk():
    md = authorization_server_metadata("example", "https://zendesk.mcp.test", ["read", "tickets:write"])
    assert md["issuer"] == "https://zendesk.mcp.test"
    assert md["authorization_endpoint"] == "https://example.zendesk.com/oauth/authorizations/new"
    assert md["token_endpoint"] == "https://example.zendesk.com/oauth/tokens"


def test_http_app_refuses_a_request_without_a_token_and_publishes_discovery():
    from starlette.testclient import TestClient

    srv = per_user.build_server()
    app = srv.streamable_http_app(host="0.0.0.0")
    with TestClient(app, base_url="http://localhost") as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        assert "resource_metadata" in r.headers.get("www-authenticate", "")
        md = c.get("/.well-known/oauth-authorization-server").json()
        assert md["token_endpoint"] == "https://example.zendesk.com/oauth/tokens"
        prm = c.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200 and prm.json()["authorization_servers"] == ["https://zendesk.mcp.test"]


def test_per_user_server_keeps_the_allowlist():
    from zendesk_mcp.tool_policy import ALLOWED_TOOLS
    srv = per_user.build_server()
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert names == ALLOWED_TOOLS


# --- server mode has no config file: the subdomain must come from the request ------------------

def test_attachment_url_check_uses_the_request_subdomain_in_server_mode():
    from zendesk_mcp.tools.attachments import _validate_attachment_url
    reset = _as_user("tok")
    try:
        assert _validate_attachment_url("https://example.zendesk.com/attachments/token/x") is None
        assert "Refusing" in _validate_attachment_url("https://evil.example.com/steal")
    finally:
        auth_context_var.reset(reset)


def test_ticket_links_use_the_request_subdomain_in_server_mode():
    from zendesk_mcp.tools.ticket import _get_subdomain
    reset = _as_user("tok")
    try:
        assert _get_subdomain() == "example"
    finally:
        auth_context_var.reset(reset)


def test_listing_asks_zendesk_for_inline_images():
    from zendesk_mcp.tools import attachments
    att = MagicMock(file_name="s.png", content_type="image/png", size=10, content_url="u", inline=True)
    client = MagicMock()
    client.tickets.comments.return_value = [MagicMock(id=1, attachments=[att])]
    with patch("zendesk_mcp.tools.attachments.get_client", return_value=client):
        out = attachments._list_attachments_data(5)
    assert client.tickets.comments.call_args.kwargs.get("include_inline_images") is True
    assert '"inline": true' in out
