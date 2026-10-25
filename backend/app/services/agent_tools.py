"""Actions an agent may take. Each tool reads/writes the app's own SQLite tables.

These are self-contained, local implementations (leave, tickets, follow-ups). To connect a real HRMS,
ITSM or CRM, replace the handler body; the schema the model sees stays the same.

Safety model
------------
* Tools that change data (MUTATING) are never executed straight from the model. They are validated with a
  dry run and parked as a pending action; the user must confirm it in the UI. This keeps instructions hidden
  in documents ("prompt injection") from silently causing actions.
* Each tool can require a permission (TOOL_PERMISSION), enforced here regardless of which assistant asks.
* Every record is scoped to the caller's organisation (org_id) and to the caller's own user id.
* Arguments from the model are untrusted: they are coerced to a dict and validated by each handler.
"""
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from app.core import rbac
from app.core.errors import AppError
from app.database.db import new_id, now
from app.database.repos import Store

log = logging.getLogger(__name__)
ANNUAL_LEAVE_DAYS = 20
LEAVE_TYPES = ["annual", "sick", "unpaid"]
MAX_LEAVE_SPAN_DAYS = 60          # calendar days in one request
MAX_LEAVE_HORIZON_DAYS = 730      # how far ahead leave may start

MUTATING = {"request_leave", "create_ticket", "request_password_reset", "schedule_followup"}
TOOL_PERMISSION = {"business_summary": "analytics.view"}

# Tools whose dates must come from the user, not from the model's imagination. Small models happily
# invent "tomorrow" for "book leave for me"; if the user's own recent messages contain no date-like
# text at all, the call is bounced back so the model has to ask.
NEEDS_USER_DATES = {"request_leave", "schedule_followup"}
DATE_HINT = re.compile(
    r"\b\d{1,4}\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b"
    r"|\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\b"
    r"|\b(today|tomorrow|tonight|next|this|week|weeks|month|months|day|days|weekend|monday|tuesday)\b"
    r"|\b(ma\wana|hoy|lunes|martes|semana|demain|lundi|semaine|morgen|montag|woche|heute)\b",
    re.IGNORECASE)


class ToolError(Exception):
    """A user-correctable problem; returned to the model as {"error": ...}."""


@dataclass
class ToolContext:
    store: Store
    user: str
    session_id: str
    org_id: str = "default"
    permissions: frozenset = rbac.ALL
    dry_run: bool = False      # validate and compute, but write nothing
    user_text: str = ""        # the user's own recent messages (never document text)


def normalize_args(raw) -> dict:
    """Model-supplied arguments are untrusted: accept an object or a JSON object string, else {}."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    try:
        json.dumps(raw)
    except (TypeError, ValueError):
        return {}
    return raw


def _fn(name: str, description: str, properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []},
    }}


def _date(value, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ToolError(f"{field} must be an ISO date like 2026-03-31") from None


def _weekdays(start: date, end: date) -> int:
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def _weekdays_by_year(start: date, end: date) -> dict[int, int]:
    out: dict[int, int] = {}
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        if d.weekday() < 5:
            out[d.year] = out.get(d.year, 0) + 1
    return out


def _year_balance(c: ToolContext, year: int) -> dict:
    """Annual leave for one calendar year; a request counts towards the year of each day it covers."""
    rows = c.store.db.all(
        "SELECT start_date, end_date, status FROM leave_requests WHERE org_id=? AND employee=? AND leave_type='annual' "
        "AND status IN ('pending','approved') AND start_date<=? AND end_date>=?",
        (c.org_id, c.user, f"{year}-12-31", f"{year}-01-01"))
    approved = pending = 0
    for r in rows:
        days = _weekdays_by_year(date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])).get(year, 0)
        if r["status"] == "approved":
            approved += days
        else:
            pending += days
    return {"year": year, "annual_entitlement": ANNUAL_LEAVE_DAYS, "approved_days": approved,
            "pending_days": pending, "remaining_days": ANNUAL_LEAVE_DAYS - approved - pending}


# -- handlers ---------------------------------------------------------------
def get_leave_balance(c: ToolContext, a: dict) -> dict:
    year = a.get("year", date.today().year)
    if not isinstance(year, int) or not 2000 <= year <= 2100:
        raise ToolError("year must be a number like 2026")
    return _year_balance(c, year)


def request_leave(c: ToolContext, a: dict) -> dict:
    start, end = _date(a.get("start_date"), "start_date"), _date(a.get("end_date"), "end_date")
    kind = a.get("leave_type") or "annual"
    if not isinstance(kind, str) or kind.lower() not in LEAVE_TYPES:
        raise ToolError(f"leave_type must be one of {LEAVE_TYPES}")
    kind = kind.lower()
    if end < start:
        raise ToolError("end_date is before start_date")
    if start < date.today():
        raise ToolError("start_date is in the past")
    if start > date.today() + timedelta(days=MAX_LEAVE_HORIZON_DAYS):
        raise ToolError(f"start_date is more than {MAX_LEAVE_HORIZON_DAYS} days ahead")
    if (end - start).days + 1 > MAX_LEAVE_SPAN_DAYS:
        raise ToolError(f"a single request may cover at most {MAX_LEAVE_SPAN_DAYS} calendar days")
    per_year = _weekdays_by_year(start, end)
    days = sum(per_year.values())
    if days == 0:
        raise ToolError("the range contains no working days")
    clash = c.store.db.one(
        "SELECT id FROM leave_requests WHERE org_id=? AND employee=? AND status IN ('pending','approved') "
        "AND start_date<=? AND end_date>=?", (c.org_id, c.user, end.isoformat(), start.isoformat()))
    if clash:
        raise ToolError("these dates overlap an existing leave request")
    if kind == "annual":
        for year, n in per_year.items():
            remaining = _year_balance(c, year)["remaining_days"]
            if n > remaining:
                raise ToolError(f"only {remaining} annual leave days remain in {year}; {n} requested")
    rid = new_id()
    if not c.dry_run:
        c.store.db.run(
            "INSERT INTO leave_requests(id,employee,leave_type,start_date,end_date,days,status,created_at,org_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (rid, c.user, kind, start.isoformat(), end.isoformat(), days, "pending", now(), c.org_id))
    return {"request_id": rid, "status": "pending", "working_days": days,
            "note": "Submitted for manager approval."}


def list_my_leave_requests(c: ToolContext, a: dict) -> dict:
    return {"requests": c.store.db.all(
        "SELECT id, leave_type, start_date, end_date, days, status FROM leave_requests "
        "WHERE org_id=? AND employee=? ORDER BY created_at DESC LIMIT 20", (c.org_id, c.user))}


def create_ticket(c: ToolContext, a: dict, kind: str = "incident") -> dict:
    title = a.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        raise ToolError("title is required")
    priority = a.get("priority") or "medium"
    if not isinstance(priority, str) or priority.lower() not in ("low", "medium", "high"):
        raise ToolError("priority must be low, medium or high")
    description = a.get("description")
    description = description if isinstance(description, str) else ""
    tid = new_id()[:8].upper()
    if not c.dry_run:
        c.store.db.run(
            "INSERT INTO tickets(id,employee,kind,title,description,priority,status,created_at,org_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (tid, c.user, kind, title[:200], description[:2000], priority.lower(), "open", now(), c.org_id))
    return {"ticket_id": tid, "status": "open", "priority": priority.lower()}


def get_ticket_status(c: ToolContext, a: dict) -> dict:
    tid = a.get("ticket_id")
    t = c.store.db.one(
        "SELECT id, kind, title, priority, status, created_at FROM tickets WHERE id=? AND org_id=? AND employee=?",
        ((tid if isinstance(tid, str) else "").upper(), c.org_id, c.user))
    if not t:
        raise ToolError("no ticket with that id for this user")
    return t


def list_my_tickets(c: ToolContext, a: dict) -> dict:
    return {"tickets": c.store.db.all(
        "SELECT id, kind, title, priority, status FROM tickets WHERE org_id=? AND employee=? "
        "ORDER BY created_at DESC LIMIT 20", (c.org_id, c.user))}


def request_password_reset(c: ToolContext, a: dict) -> dict:
    system = a.get("system")
    system = system.strip() if isinstance(system, str) and system.strip() else "corporate account"
    out = create_ticket(c, {"title": f"Password reset: {system[:100]}", "priority": "high",
                            "description": "Requested via assistant."}, kind="password_reset")
    out["note"] = "A password-reset request was logged for the IT team; no password was changed."
    return out


def schedule_followup(c: ToolContext, a: dict) -> dict:
    contact = a.get("contact")
    contact = contact.strip() if isinstance(contact, str) else ""
    if not contact:
        raise ToolError("contact is required")
    due = _date(a.get("due_date"), "due_date")
    if due < date.today():
        raise ToolError("due_date is in the past")
    note = a.get("note")
    fid = new_id()[:8].upper()
    if not c.dry_run:
        c.store.db.run("INSERT INTO followups(id,owner,contact,note,due_date,status,created_at,org_id) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (fid, c.user, contact[:200], (note if isinstance(note, str) else "")[:1000],
                        due.isoformat(), "scheduled", now(), c.org_id))
    return {"followup_id": fid, "contact": contact[:200], "due_date": due.isoformat()}


def list_followups(c: ToolContext, a: dict) -> dict:
    return {"followups": c.store.db.all(
        "SELECT id, contact, note, due_date, status FROM followups WHERE org_id=? AND owner=? "
        "ORDER BY due_date LIMIT 20", (c.org_id, c.user))}


def business_summary(c: ToolContext, a: dict) -> dict:
    return {**c.store.business_summary(c.org_id), "as_of": datetime.now().isoformat(timespec="minutes")}


def request_human_agent(c: ToolContext, a: dict) -> dict:
    reason = a.get("reason")
    try:
        h = c.store.create_handoff(c.session_id, reason if isinstance(reason, str) else "")
    except AppError as exc:
        raise ToolError(exc.message) from exc
    return {"handoff_id": h["id"], "status": h["status"],
            "note": "A human agent has been asked to join this conversation."}


TOOLS: dict[str, tuple[dict, Callable[[ToolContext, dict], dict]]] = {
    "get_leave_balance": (_fn(
        "get_leave_balance", "Get the current user's annual leave balance (optionally for a given year).",
        {"year": {"type": "integer"}}), get_leave_balance),
    "request_leave": (_fn(
        "request_leave", "Prepare a leave request for the current user (the user must confirm it). "
        "Dates are ISO (YYYY-MM-DD). Ask the user for any missing date first.",
        {"start_date": {"type": "string"}, "end_date": {"type": "string"},
         "leave_type": {"type": "string", "enum": LEAVE_TYPES}}, ["start_date", "end_date"]), request_leave),
    "list_my_leave_requests": (_fn("list_my_leave_requests", "List the current user's recent leave requests."),
                               list_my_leave_requests),
    "create_ticket": (_fn(
        "create_ticket", "Prepare an IT support ticket for the current user (the user must confirm it).",
        {"title": {"type": "string"}, "description": {"type": "string"},
         "priority": {"type": "string", "enum": ["low", "medium", "high"]}}, ["title"]), create_ticket),
    "get_ticket_status": (_fn("get_ticket_status", "Look up one of the user's tickets by id.",
                              {"ticket_id": {"type": "string"}}, ["ticket_id"]), get_ticket_status),
    "list_my_tickets": (_fn("list_my_tickets", "List the current user's tickets."), list_my_tickets),
    "request_password_reset": (_fn(
        "request_password_reset",
        "Prepare a password-reset request for the IT team (the user must confirm it; no password is changed).",
        {"system": {"type": "string"}}), request_password_reset),
    "schedule_followup": (_fn(
        "schedule_followup", "Prepare a follow-up with a customer or lead (the user must confirm it).",
        {"contact": {"type": "string"}, "note": {"type": "string"}, "due_date": {"type": "string"}},
        ["contact", "due_date"]), schedule_followup),
    "list_followups": (_fn("list_followups", "List scheduled follow-ups."), list_followups),
    "business_summary": (_fn("business_summary", "Current operational numbers: tickets, leave, handoffs, documents."),
                         business_summary),
    "request_human_agent": (_fn(
        "request_human_agent",
        "Escalate to a human agent when the user asks for one or the assistant cannot help.",
        {"reason": {"type": "string"}}), request_human_agent),
}

LABELS = {
    "request_leave": "Submit leave request", "create_ticket": "Create IT ticket",
    "request_password_reset": "Log password-reset request", "schedule_followup": "Schedule follow-up",
}


def describe(name: str, args: dict) -> str:
    """Human-readable one-liner shown to the user when they are asked to confirm an action."""
    args = normalize_args(args)
    parts = ", ".join(f"{k}: {str(v)[:80]}" for k, v in args.items() if v not in (None, ""))
    return f"{LABELS.get(name, name)}" + (f" ({parts})" if parts else "")


def specs_for(names: list[str]) -> list[dict]:
    """Tool schemas for an agent; human escalation is always available."""
    wanted = [n for n in names if n in TOOLS]
    if "request_human_agent" not in wanted:
        wanted.append("request_human_agent")
    return [TOOLS[n][0] for n in wanted]


def _execute(ctx: ToolContext, name: str, args: dict) -> tuple[bool, dict]:
    try:
        return True, TOOLS[name][1](ctx, args)
    except ToolError as exc:
        return False, {"error": str(exc)}
    except Exception:
        log.exception("tool %s failed", name)
        return False, {"error": "tool failed unexpectedly"}


def _allowed(ctx: ToolContext, name: str) -> bool:
    return TOOL_PERMISSION.get(name, "chat.use") in ctx.permissions


def run_tool(ctx: ToolContext, name: str, args, allowed: list[str],
             confirm_actions: bool = True) -> tuple[bool, dict]:
    """Run a model-requested tool for an allowed agent. Returns (ok, result).

    Mutating tools return a pending-confirmation result instead of executing (see module docstring).
    """
    if name not in TOOLS or (name not in allowed and name != "request_human_agent"):
        return False, {"error": f"unknown or unavailable tool '{name}'"}
    if not _allowed(ctx, name):
        return False, {"error": f"your role is not allowed to use '{name}'"}
    args = normalize_args(args)
    if name in NEEDS_USER_DATES and not DATE_HINT.search(ctx.user_text):
        return False, {"error": "The user has not said when yet. Do not guess dates: ask the user which "
                                "dates they want, then call the tool again."}
    if name in MUTATING and confirm_actions:
        ok, preview = _execute(replace(ctx, dry_run=True), name, args)
        if not ok:
            return False, preview
        action_id = ctx.store.create_action(ctx.session_id, ctx.user, name, args)
        return True, {"status": "awaiting_user_confirmation", "action_id": action_id,
                      "summary": describe(name, args),
                      "note": "NOT executed yet. Tell the user to review the action card and press Confirm."}
    return _execute(ctx, name, args)


def execute_confirmed(ctx: ToolContext, name: str, args: dict) -> tuple[bool, dict]:
    """Execute an action the user has explicitly confirmed (re-validated at execution time)."""
    if name not in TOOLS or name not in MUTATING:
        return False, {"error": f"'{name}' cannot be confirmed"}
    if not _allowed(ctx, name):
        return False, {"error": f"your role is not allowed to use '{name}'"}
    return _execute(ctx, name, normalize_args(args))
