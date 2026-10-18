"""Boom fork: per-user Zendesk access over streamable HTTP.

Each person signs in to Zendesk themselves when they connect the claude.ai connector. claude.ai
holds and refreshes their Zendesk OAuth token and sends it as a bearer token on every request;
this server checks it and makes every Zendesk call as that person. Nothing is stored here.

* The authorization server is Zendesk. Zendesk publishes no OAuth discovery document, so this
  server publishes one (``/.well-known/oauth-authorization-server``) whose endpoints are
  Zendesk's own authorize and token URLs. The SDK publishes the protected-resource document and
  answers 401 with ``WWW-Authenticate`` when a token is missing or refused.
* A token is accepted only for an active, unsuspended Zendesk **agent or admin**, and (if
  ``ZENDESK_MCP_ALLOWED_EMAIL_DOMAINS`` is set) only for those email domains. A customer who has
  a Zendesk login is refused.
* The tool allowlist in ``tool_policy`` still applies: whatever a person's Zendesk role allows,
  this server can only read and add internal notes.

Configuration (environment):
  ZENDESK_SUBDOMAIN                  e.g. boomsupport
  ZENDESK_MCP_BASE_URL               public URL of this server, e.g. https://zendesk.mcp.example
  ZENDESK_MCP_ALLOWED_EMAIL_DOMAINS  comma-separated, e.g. boom.limited (empty = any domain)
  ZENDESK_MCP_SCOPES                 scopes requested at sign-in (default "read tickets:write hc:write")
  ZENDESK_MCP_NAME, ZENDESK_MCP_INSTRUCTIONS   the MCP server name and instructions for Claude
"""
from __future__ import annotations

import hashlib
import os
import sys
import time

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

ALLOWED_ROLES = frozenset({"agent", "admin"})
CACHE_OK_SECONDS = 300
CACHE_REFUSED_SECONDS = 30


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def subdomain() -> str:
    sub = _env("ZENDESK_SUBDOMAIN")
    if not sub:
        raise RuntimeError("ZENDESK_SUBDOMAIN is not set")
    return sub


def base_url() -> str:
    url = _env("ZENDESK_MCP_BASE_URL").rstrip("/")
    if not url:
        raise RuntimeError("ZENDESK_MCP_BASE_URL is not set")
    return url


def scopes() -> list[str]:
    return _env("ZENDESK_MCP_SCOPES", "read tickets:write hc:write").split()


def allowed_domains() -> frozenset[str]:
    return frozenset(d.strip().lower().lstrip("@") for d in _env("ZENDESK_MCP_ALLOWED_EMAIL_DOMAINS").split(",") if d.strip())


def admit(user: dict, domains: frozenset[str]) -> str | None:
    """Return None if this Zendesk user may use the server, else the reason they may not."""
    if not user or not user.get("id"):
        return "no user"
    if user.get("role") not in ALLOWED_ROLES:
        return f"role {user.get('role')!r} is not agent or admin"
    if not user.get("active", False) or user.get("suspended", False):
        return "user is inactive or suspended"
    if domains:
        email = (user.get("email") or "").lower()
        if email.rpartition("@")[2] not in domains:
            return "email domain not allowed"
    return None


class ZendeskTokenVerifier:
    """Accepts a bearer token if Zendesk says it belongs to an allowed agent or admin."""

    def __init__(self, sub: str | None = None, domains: frozenset[str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._sub = sub
        self._domains = domains
        self._transport = transport
        self._cache: dict[str, tuple[float, AccessToken | None]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]

        sub = self._sub or subdomain()
        domains = allowed_domains() if self._domains is None else self._domains
        result: AccessToken | None = None
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=15) as client:
                resp = await client.get(f"https://{sub}.zendesk.com/api/v2/users/me.json",
                                        headers={"Authorization": f"Bearer {token}"})
            user = resp.json().get("user", {}) if resp.status_code == 200 else {}
            reason = admit(user, domains)
            if reason is None:
                result = AccessToken(token=token, client_id="zendesk", scopes=scopes(),
                                     subject=str(user["id"]),
                                     claims={"email": user.get("email"), "role": user.get("role")})
            else:
                # stdout carries nothing here (HTTP transport), but keep diagnostics on stderr.
                print(f"zendesk-mcp: refused token (HTTP {resp.status_code}): {reason}", file=sys.stderr)
        except httpx.HTTPError as e:
            print(f"zendesk-mcp: could not verify token with Zendesk: {e}", file=sys.stderr)
            return None  # a network failure is not cached

        self._cache[key] = (now + (CACHE_OK_SECONDS if result else CACHE_REFUSED_SECONDS), result)
        return result


def authorization_server_metadata(sub: str, base: str, scope_list: list[str]) -> dict:
    zd = f"https://{sub}.zendesk.com"
    return {
        "issuer": base,
        "authorization_endpoint": f"{zd}/oauth/authorizations/new",
        "token_endpoint": f"{zd}/oauth/tokens",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "scopes_supported": scope_list,
    }


def build_server():
    from zendesk_mcp.server import FastMCP, register_all

    base = base_url()
    srv = FastMCP(
        _env("ZENDESK_MCP_NAME", "zendesk-mcp"),
        instructions=_env("ZENDESK_MCP_INSTRUCTIONS") or None,
        token_verifier=ZendeskTokenVerifier(),
        auth=AuthSettings(issuer_url=base, resource_server_url=f"{base}/mcp", required_scopes=scopes(),
                          # Zendesk tokens carry no resource or audience; verify_token checks them with Zendesk.
                          validate_token_resource=False),
    )
    register_all(srv)

    @srv.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def _as_metadata(request: Request) -> JSONResponse:
        return JSONResponse(authorization_server_metadata(subdomain(), base, scopes()))

    @srv.custom_route("/health", methods=["GET"])
    async def _health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return srv


def serve(host: str = "0.0.0.0", port: int = 3000) -> None:
    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    srv = build_server()
    public_host = base_url().split("://", 1)[1].split("/", 1)[0]
    app = srv.streamable_http_app(
        host=host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_host, "localhost", f"localhost:{port}"],
            allowed_origins=[f"https://{public_host}", "https://claude.ai", "https://claude.com"],
        ),
    )
    uvicorn.run(app, host=host, port=int(os.environ.get("PORT", port)), proxy_headers=True)
