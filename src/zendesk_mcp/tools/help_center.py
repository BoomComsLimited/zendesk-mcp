"""Boom fork: help-centre (Zendesk Guide) articles: read freely, write only as drafts.

Reading: sections, article search and listing, and a full article, so Claude can look at real
examples before writing.

Writing never touches what customers can see:
* New articles are always created as drafts.
* An existing article is only edited while its translation is still a draft. Editing a
  published article's text changes the live article at once, so a published article is
  refused; zendesk_propose_article_update instead creates a separate draft ("PROPOSED UPDATE:
  ...") that links the original, for a person to merge and publish in Guide.
* Inkbot found that API edits broke articles upgraded to Guide's newer article format. Whether
  that still happens is being tested (2026-10-18) on drafts only, where a breakage reaches no
  customer.
* There is no tool to publish, unpublish, delete or move an article.

Every call is made with the signed-in person's token (per-user mode), so their own Guide
permissions apply on top of this.

Environment: ZENDESK_LOCALE (default en-gb), ZENDESK_MCP_PERMISSION_GROUP_ID (optional; else the
first non-built-in group that can edit), ZENDESK_MCP_USER_SEGMENT_ID (optional; default none).
"""
import html
import json
import os

from zendesk_mcp import auth
from zendesk_mcp.auth import api_error_message, TokenExpiredError
from zendesk_mcp.client import get_oauth_session, ConfigError

PROPOSED_PREFIX = "PROPOSED UPDATE: "


def _locale() -> str:
    return os.environ.get("ZENDESK_LOCALE", "en-gb").strip() or "en-gb"


def _base() -> str:
    subdomain, _ = get_oauth_session()
    return f"https://{subdomain}.zendesk.com/api/v2/help_center"


def _summary(a: dict) -> dict:
    return {
        "id": a.get("id"), "title": a.get("title"), "section_id": a.get("section_id"),
        "draft": a.get("draft"), "updated_at": a.get("updated_at"),
        "labels": a.get("label_names", []), "url": a.get("html_url"),
    }


def _get(url: str, **params) -> dict:
    resp = auth.request("GET", url, params=params or None)
    resp.raise_for_status()
    return resp.json()


def _permission_group_id(base: str) -> int:
    env = os.environ.get("ZENDESK_MCP_PERMISSION_GROUP_ID", "").strip()
    if env:
        return int(env)
    groups = _get(f"{base}/guide/permission_groups.json").get("permission_groups", [])
    for pick in (lambda g: not g.get("built_in") and (g.get("edit") or g.get("publish")),
                 lambda g: g.get("edit") or g.get("publish"), lambda g: True):
        for g in groups:
            if pick(g):
                return int(g["id"])
    raise ValueError("No Guide permission group is available; set ZENDESK_MCP_PERMISSION_GROUP_ID.")


def _user_segment_id():
    env = os.environ.get("ZENDESK_MCP_USER_SEGMENT_ID", "").strip()
    return int(env) if env else None


def _create_draft(section_id: int, title: str, body_html: str, label_names: list[str] | None = None) -> dict:
    base = _base()
    article = {
        "title": title, "body": body_html, "locale": _locale(),
        "draft": True,  # Boom fork: always a draft. Never change this.
        "permission_group_id": _permission_group_id(base),
        "user_segment_id": _user_segment_id(),
    }
    if label_names:
        article["label_names"] = label_names
    resp = auth.request("POST", f"{base}/{_locale()}/sections/{section_id}/articles.json", json={"article": article})
    resp.raise_for_status()
    return resp.json()["article"]


def _translation(base: str, article_id: int) -> dict:
    return _get(f"{base}/articles/{article_id}/translations/{_locale()}.json").get("translation", {})


def _safe(fn):
    try:
        return fn()
    except (ConfigError, TokenExpiredError) as e:
        return str(e)
    except Exception as e:
        if "404" in str(e):
            return "Not found, or not visible to your Zendesk account."
        return api_error_message(e)


def register_help_center_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_list_help_center_sections() -> str:
        """List help-centre sections with their category, so you can choose where a draft article belongs."""
        def run():
            base = _base()
            cats = {c["id"]: c["name"] for c in _get(f"{base}/{_locale()}/categories.json", per_page=100).get("categories", [])}
            secs = _get(f"{base}/{_locale()}/sections.json", per_page=100).get("sections", [])
            return json.dumps([{"id": s["id"], "name": s["name"], "category": cats.get(s.get("category_id"))} for s in secs], indent=2)
        return _safe(run)

    @mcp.tool()
    def zendesk_list_articles(query: str = "", section_id: int = 0, limit: int = 25) -> str:
        """Find help-centre articles. query: full-text search (published articles). section_id: list one
        section's articles, including drafts you can see. With neither, the most recently updated articles.
        Use zendesk_get_article to read one in full, e.g. as an example of house style before writing."""
        def run():
            base = _base()
            limit_ = max(1, min(int(limit), 100))
            if query.strip():
                data = _get(f"{base}/articles/search.json", query=query.strip(), locale=_locale(), per_page=limit_)
                items = data.get("results", [])
            elif section_id:
                items = _get(f"{base}/{_locale()}/sections/{section_id}/articles.json", per_page=limit_).get("articles", [])
            else:
                items = _get(f"{base}/{_locale()}/articles.json", sort_by="updated_at", sort_order="desc", per_page=limit_).get("articles", [])
            return json.dumps([_summary(a) for a in items[:limit_]], indent=2)
        return _safe(run)

    @mcp.tool()
    def zendesk_get_article(article_id: int) -> str:
        """Read one help-centre article in full: title, HTML body, section, labels, and whether it is a draft."""
        def run():
            a = _get(f"{_base()}/{_locale()}/articles/{article_id}.json")["article"]
            return json.dumps({**_summary(a), "body_html": a.get("body"),
                               "user_segment_id": a.get("user_segment_id"),
                               "permission_group_id": a.get("permission_group_id")}, indent=2)
        return _safe(run)

    @mcp.tool()
    def zendesk_create_article_draft(section_id: int, title: str, body_html: str, label_names: list[str] | None = None) -> str:
        """Create a new help-centre article as a DRAFT in the given section. It is not visible to customers
        until a person publishes it in Zendesk Guide. body_html is the article body in HTML."""
        def run():
            a = _create_draft(section_id, title, body_html, label_names)
            return f"Draft article {a['id']} created in section {section_id}: {a.get('html_url')}. A person must review and publish it in Guide."
        return _safe(run)

    @mcp.tool()
    def zendesk_update_article_draft(article_id: int, title: str = "", body_html: str = "") -> str:
        """Edit an article that is still a DRAFT. Refuses a published article, because editing published
        text changes the live article; use zendesk_propose_article_update for those."""
        def run():
            base = _base()
            if not _translation(base, article_id).get("draft"):
                return (f"Article {article_id} is published, so it was not changed. Use zendesk_propose_article_update "
                        f"to create a separate draft for a person to review and merge.")
            changes = {"draft": True}  # Boom fork: keep it a draft. Never send draft=False.
            if title:
                changes["title"] = title
            if body_html:
                changes["body"] = body_html
            resp = auth.request("PUT", f"{base}/articles/{article_id}/translations/{_locale()}.json", json={"translation": changes})
            resp.raise_for_status()
            return f"Draft article {article_id} updated. It is still a draft."
        return _safe(run)

    @mcp.tool()
    def zendesk_propose_article_update(article_id: int, body_html: str, title: str = "", note: str = "") -> str:
        """Propose changes to a PUBLISHED article without touching it: creates a separate draft titled
        'PROPOSED UPDATE: <title>' in the same section, starting with a link to the original, for a person
        to merge and publish in Guide. note: optional summary of what changed and why."""
        def run():
            base = _base()
            orig = _get(f"{base}/{_locale()}/articles/{article_id}.json")["article"]
            header = (f"<p><strong>Proposed update to <a href=\"{html.escape(orig.get('html_url') or '')}\">"
                      f"{html.escape(orig.get('title') or str(article_id))}</a> (article {article_id}).</strong> "
                      f"Merge into the original and publish there; then delete this draft.</p>")
            if note:
                header += f"<p><em>{html.escape(note)}</em></p>"
            a = _create_draft(orig["section_id"], PROPOSED_PREFIX + (title or orig.get("title") or ""), header + "<hr>" + body_html)
            return f"Proposed update created as draft article {a['id']}: {a.get('html_url')}. The original article {article_id} was not changed."
        return _safe(run)
