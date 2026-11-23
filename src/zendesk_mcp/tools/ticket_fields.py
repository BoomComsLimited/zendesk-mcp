"""Boom fork: read the ticket field definitions, and fill a ticket's triage fields.

``zendesk_set_ticket_fields`` is deliberately narrow, for Neo's first triage of a new ticket:
- it sets only the ticket's Type, Priority and custom fields of type dropdown ("tagger"),
  multi-select and single-line text. Never status, assignee, requester, group, subject,
  comments, tags directly, or any other field;
- every value must be one of the field's own options (dropdown / multi-select) or a short
  single line of text;
- it only FILLS: a field that already has a value is left alone and reported as skipped, so an
  agent's choice is never overwritten;
- it is all or nothing: one invalid field or value and nothing is changed;
- the update is a safe update (Zendesk rejects it if the ticket changed since it was read).

Zendesk adds and removes the matching tags itself when a dropdown or multi-select changes.
"""
import json

from zendesk_mcp import auth
from zendesk_mcp.auth import api_error_message, TokenExpiredError
from zendesk_mcp.client import get_oauth_session, ConfigError

SETTABLE_TYPES = {"tagger", "multiselect", "text"}
TICKET_TYPES = {"problem", "incident", "question", "task"}
PRIORITIES = {"low", "normal", "high", "urgent"}
MAX_TEXT = 255


def _base() -> str:
    subdomain, _ = get_oauth_session()
    return f"https://{subdomain}.zendesk.com/api/v2"


def _fields(base: str) -> list:
    """Every ticket field (all pages)."""
    out, url = [], f"{base}/ticket_fields?page[size]=100"
    while url:
        response = auth.request("GET", url, timeout=30)
        response.raise_for_status()
        data = response.json()
        out += data.get("ticket_fields", [])
        url = data.get("links", {}).get("next") if data.get("meta", {}).get("has_more") else None
    return out


def _describe(f: dict) -> dict:
    d = {"id": f.get("id"), "title": f.get("title"), "type": f.get("type"),
         "required": bool(f.get("required")), "description": f.get("description") or None}
    options = f.get("custom_field_options") or f.get("system_field_options")
    if options:
        d["options"] = [{"name": o.get("name"), "value": o.get("value")} for o in options]
    return {k: v for k, v in d.items() if v not in (None, "")}


def _ticket_fields_data() -> str:
    try:
        base = _base()
        fields = [f for f in _fields(base) if f.get("active")]
        return json.dumps([_describe(f) for f in fields], indent=2)
    except (ConfigError, TokenExpiredError) as e:
        return str(e)
    except Exception as e:
        return api_error_message(e)


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value is False


def _check_value(field: dict, value):
    """The value to send, or raise ValueError."""
    kind, title = field.get("type"), field.get("title")
    allowed = {o.get("value") for o in field.get("custom_field_options") or []}
    if kind == "tagger":
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f"{title!r}: {value!r} isn't one of its options")
        return value
    if kind == "multiselect":
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not values or any(v not in allowed for v in values):
            raise ValueError(f"{title!r}: {value!r} must be one or more of its options")
        return list(dict.fromkeys(values))
    if kind == "text":
        if not isinstance(value, str) or not value.strip() or "\n" in value or len(value) > MAX_TEXT:
            raise ValueError(f"{title!r}: text must be one non-empty line of at most {MAX_TEXT} characters")
        return value.strip()
    raise ValueError(f"{title!r} is a {kind} field, which this tool can't set")


def _set_ticket_fields_data(ticket_id: int, type: str | None = None, priority: str | None = None,
                            fields: dict | None = None) -> str:
    fields = fields or {}
    if not isinstance(fields, dict):
        return "fields must be an object of {field id: value}"
    if not (type or priority or fields):
        return "Nothing to set: give type, priority or fields."
    try:
        base = _base()
        defs = {str(f.get("id")): f for f in _fields(base) if f.get("active")}
        response = auth.request("GET", f"{base}/tickets/{int(ticket_id)}", timeout=30)
        if response.status_code == 404:
            return f"Ticket #{ticket_id} not found or not accessible with current credentials."
        response.raise_for_status()
        ticket = response.json().get("ticket", {})
        current = {str(c.get("id")): c.get("value") for c in ticket.get("custom_fields") or []}

        update, done, skipped = {}, {}, {}
        if type is not None:
            if type not in TICKET_TYPES:
                return f"Invalid type {type!r}. Valid: {', '.join(sorted(TICKET_TYPES))}. Nothing was changed."
            if _empty(ticket.get("type")):
                update["type"] = done["type"] = type
            else:
                skipped["type"] = ticket.get("type")
        if priority is not None:
            if priority not in PRIORITIES:
                return f"Invalid priority {priority!r}. Valid: {', '.join(sorted(PRIORITIES))}. Nothing was changed."
            if _empty(ticket.get("priority")):
                update["priority"] = done["priority"] = priority
            else:
                skipped["priority"] = ticket.get("priority")
        custom = []
        for fid, value in fields.items():
            field = defs.get(str(fid))
            if field is None:
                return f"Field {fid} isn't an active ticket field. Nothing was changed."
            if field.get("type") not in SETTABLE_TYPES:
                return f"{field.get('title')!r} is a {field.get('type')} field, which this tool can't set. Nothing was changed."
            try:
                value = _check_value(field, value)
            except ValueError as e:
                return f"{e}. Nothing was changed."
            if _empty(current.get(str(fid))):
                custom.append({"id": field["id"], "value": value})
                done[field.get("title")] = value
            else:
                skipped[field.get("title")] = current.get(str(fid))
        if custom:
            update["custom_fields"] = custom
        if update:
            update["safe_update"] = True
            update["updated_stamp"] = ticket.get("updated_at")
            r = auth.request("PUT", f"{base}/tickets/{int(ticket_id)}", json={"ticket": update}, timeout=30)
            if r.status_code == 409:
                return f"Ticket #{ticket_id} changed while it was being updated. Nothing was changed; try again."
            r.raise_for_status()
        return json.dumps({"ticket_id": int(ticket_id), "set": done, "already_set": skipped}, indent=2)
    except (ConfigError, TokenExpiredError) as e:
        return str(e)
    except Exception as e:
        return api_error_message(e)


def register_ticket_field_tools(mcp) -> None:
    @mcp.tool()
    def zendesk_get_ticket_fields() -> str:
        """List the active ticket fields: id, title, type, whether required, and for dropdown and multi-select fields their options ({name, value}). Use the option value (not the name) when setting a field."""
        return _ticket_fields_data()

    @mcp.tool()
    def zendesk_set_ticket_fields(ticket_id: int, type: str | None = None, priority: str | None = None,
                                  fields: dict | None = None) -> str:
        """Fill a ticket's triage fields: type (problem/incident/question/task), priority (low/normal/high/urgent), and custom dropdown, multi-select or single-line text fields as {field id: value} (option values from zendesk_get_ticket_fields; a list for multi-select). Only fills empty fields: anything already set is left alone and reported in already_set. All or nothing: an invalid field or value changes nothing. Can't change status, assignee, requester, group, subject or comments."""
        return _set_ticket_fields_data(ticket_id, type=type, priority=priority, fields=fields)
