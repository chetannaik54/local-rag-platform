"""Authentication and authorisation.

Who is calling?
  * auth disabled (default, single local user): a super admin in the default organisation.
  * ``Authorization: Bearer ses_...``  a login session (email + password).
  * ``Authorization: Bearer rag_...`` or ``X-API-Key``: a user's API key.
  * ``X-Embed-Key: pk_...`` + ``X-Visitor-Id``: an anonymous visitor of an embedded widget (chat only,
    only from origins allowed for that key).

What may they do?  Every endpoint declares a permission (``require_perm``); roles are sets of permissions.
A super admin (``platform.manage``) may act inside any organisation with ``X-Org-Id``.
"""
import logging
import re
import secrets
from dataclasses import dataclass, field

from fastapi import Request

from app.core import rbac
from app.core.errors import AppError
from app.core.security import hash_password
from app.database.repos import DEFAULT_ORG, random_password

log = logging.getLogger(__name__)
_VISITOR_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_ORIGIN = re.compile(r"^https?://[a-z0-9.-]+(:\d{1,5})?$")


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    org_id: str
    role: str
    permissions: frozenset[str]
    kind: str = "user"                 # user | local | visitor
    email: str | None = None
    embed_key_id: str | None = None
    embed_agent: str | None = None
    token: str | None = field(default=None, repr=False)

    @property
    def is_super(self) -> bool:
        return "platform.manage" in self.permissions

    def can(self, *perms: str) -> bool:
        return all(p in self.permissions for p in perms)


def normalize_origin(origin: str) -> str:
    return origin.strip().lower().rstrip("/")


def valid_origin_pattern(pattern: str) -> bool:
    """'*' (anywhere), 'https://site.com[:port]' or 'https://*.site.com'."""
    p = normalize_origin(pattern)
    return p == "*" or bool(_ORIGIN.match(p.replace("*.", "x.", 1)) and p.count("*") <= 1)


def origin_allowed(origin: str | None, patterns: list[str]) -> bool:
    pats = [normalize_origin(p) for p in patterns]
    if "*" in pats:
        return True
    if not origin:
        return False
    o = normalize_origin(origin)
    if not _ORIGIN.match(o):
        return False
    for p in pats:
        if "*" in p:
            rx = "^" + re.escape(p).replace(r"\*", r"[a-z0-9-]+(\.[a-z0-9-]+)*") + "$"
            if re.match(rx, o):
                return True
        elif p == o:
            return True
    return False


def bootstrap_admin(sv) -> None:
    """With auth on, make sure a platform super admin exists and can log in."""
    if not sv.config.auth_enabled:
        return
    store, cfg = sv.store, sv.config
    wrote: list[str] = []
    admin = (store.super_admins() or [None])[0]
    token = None
    if admin is None:
        admin, token = store.create_user(DEFAULT_ORG, "admin", "super_admin", token=cfg.admin_token or None)
    if not admin["has_password"]:
        email = (cfg.admin_email or "admin@localhost").strip().lower()
        if store.user_by_email(email) and store.user_by_email(email)["id"] != admin["id"]:
            email = f"admin+{secrets.token_hex(2)}@localhost"
        password = cfg.admin_password or random_password()
        store.update_user(admin["id"], email=email, password_hash=hash_password(password))
        if not cfg.admin_password:
            path = cfg.data_dir / "admin_credentials.txt"
            path.write_text(f"email: {email}\npassword: {password}\n", encoding="utf-8")
            wrote.append(str(path))
    if token and not cfg.admin_token:
        path = cfg.data_dir / "admin_token.txt"
        path.write_text(token + "\n", encoding="utf-8")
        wrote.append(str(path))
    # Records made in single-user mode belong to the implicit "local" user; hand them to the platform admin
    # so the first sign-in shows the existing chats instead of an empty app.
    for table, col in (("sessions", "owner"), ("pending_actions", "owner"), ("tickets", "employee"),
                       ("leave_requests", "employee"), ("followups", "owner")):
        store.db.run(f"UPDATE {table} SET {col}=? WHERE {col}='local' AND org_id=?", (admin["id"], DEFAULT_ORG))
    if wrote:
        log.warning("created the platform admin; credentials saved to %s", ", ".join(wrote))


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _fail(sv, request: Request, token: str | None, code: str, message: str, status: int) -> AppError:
    if token:  # a missing credential is just an anonymous request; a wrong one is a guess
        sv.limits.record(f"authfail:{_client_ip(request)}")
    return AppError(code, message, status)


def principal_for(request: Request) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    p = _resolve(request)
    request.state.principal = p
    return p


def _resolve(request: Request) -> Principal:
    sv = request.app.state.services
    cfg, store = sv.config, sv.store
    org_override = request.headers.get("x-org-id", "").strip()

    if not cfg.auth_enabled:  # single local user: full rights, optionally "inside" another organisation
        org = org_override or DEFAULT_ORG
        if not store.get_org(org):
            raise AppError("not_found", "Organization not found.", 404)
        return Principal("local", "local", org, "super_admin", rbac.ALL, kind="local")

    ip_key = f"authfail:{_client_ip(request)}"
    if sv.limits.over(ip_key, cfg.auth_failures_per_minute):
        raise AppError("rate_limited", "Too many failed sign-in attempts. Try again in a minute.", 429)

    embed_token = request.headers.get("x-embed-key", "").strip()
    if embed_token:
        return _visitor(sv, request, embed_token)

    token = _bearer(request)
    user = None
    if token:
        user = (store.user_by_session(token) if token.startswith("ses_") else None) or store.user_by_token(token)
    if not user:
        raise _fail(sv, request, token, "unauthorized", "Missing or invalid credentials.", 401)
    if not user["active"]:
        raise AppError("account_disabled", "This account has been disabled.", 403)
    perms = frozenset(user["permissions"])
    if not user["org_active"] and "platform.manage" not in perms:
        raise AppError("org_disabled", "This organization has been disabled.", 403)
    org_id = user["org_id"]
    if org_override and org_override != org_id:
        if "platform.manage" not in perms:
            raise AppError("forbidden", "You cannot act inside another organization.", 403)
        if not store.get_org(org_override):
            raise AppError("not_found", "Organization not found.", 404)
        org_id = org_override
    return Principal(user["id"], user["name"], org_id, user["role_name"], perms, email=user["email"],
                     token=token if token.startswith("ses_") else None)


def _visitor(sv, request: Request, embed_token: str) -> Principal:
    store = sv.store
    key = store.embed_key_by_token(embed_token)
    if not key:
        raise _fail(sv, request, embed_token, "unauthorized", "Invalid embed key.", 401)
    org = store.get_org(key["org_id"])
    if not key["active"] or not org or not org["active"]:
        raise AppError("embed_disabled", "This chat widget is disabled.", 403)
    if not origin_allowed(request.headers.get("origin"), key["allowed_origins"]):
        raise AppError("origin_not_allowed", "This website is not allowed to use this chat widget.", 403)
    visitor = request.headers.get("x-visitor-id", "").strip()
    if not _VISITOR_ID.match(visitor):
        raise AppError("validation_error", "X-Visitor-Id must be 8-64 letters, digits, '-' or '_'.", 422)
    if sv.limits.hit(f"embedtouch:{key['id']}", 1, window=60.0):
        store.touch_embed_key(key["id"])
    return Principal(f"v:{key['id']}:{visitor}", f"visitor {visitor[:6]}", key["org_id"], "visitor",
                     rbac.VISITOR_PERMISSIONS, kind="visitor", embed_key_id=key["id"], embed_agent=key["agent_id"])


def require_perm(*perms: str):
    """FastAPI dependency factory: ``Depends(require_perm("documents.write"))``."""
    for p in perms:
        assert p in rbac.PERMISSIONS, f"unknown permission {p}"

    def dep(request: Request) -> Principal:
        who = principal_for(request)
        missing = [p for p in perms if p not in who.permissions]
        if missing:
            raise AppError("forbidden", f"This action needs the '{missing[0]}' permission.", 403)
        return who
    return dep


def require_any(*perms: str):
    """Allow the call if the caller holds at least one of ``perms``."""
    for p in perms:
        assert p in rbac.PERMISSIONS, f"unknown permission {p}"

    def dep(request: Request) -> Principal:
        who = principal_for(request)
        if not any(p in who.permissions for p in perms):
            raise AppError("forbidden", f"This action needs one of: {', '.join(perms)}.", 403)
        return who
    return dep


def authenticated(request: Request) -> Principal:
    """Any signed-in caller (including visitors), no particular permission."""
    return principal_for(request)
