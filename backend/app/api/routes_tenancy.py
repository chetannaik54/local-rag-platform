"""Organizations (tenants), roles & users (RBAC), website embed keys and platform settings."""
import sqlite3

from fastapi import APIRouter, Depends, Request, Response

from app.api.auth import (
    Principal,
    normalize_origin,
    principal_for,
    require_any,
    require_perm,
    valid_origin_pattern,
)
from app.api.routes import svc
from app.core import rbac
from app.core.errors import AppError
from app.core.security import check_password_policy, hash_password, normalize_email
from app.models import schemas as s

router = APIRouter(prefix="/api")


# ======================================================================= RBAC: roles & users
def _forbid_escalation(who: Principal, perms: set[str] | frozenset[str]) -> None:
    """Nobody can hand out (or touch) power they do not have themselves."""
    extra = sorted(set(perms) - who.permissions)
    if extra:
        raise AppError("forbidden", "You cannot grant or change permissions you do not hold yourself: "
                       + ", ".join(extra) + ".", 403)


def _role_out(r: dict) -> s.RoleOut:
    return s.RoleOut(id=r["id"], name=r["name"], builtin=r["builtin"], permissions=r["permissions"],
                     user_count=r.get("user_count", 0))


def _user_out(u: dict) -> s.UserOut:
    return s.UserOut(id=u["id"], name=u["name"], email=u["email"], role=u["role_name"], role_id=u["role_id"],
                     active=u["active"], has_password=u["has_password"], has_api_key=u["has_api_key"],
                     created_at=u["created_at"])


@router.get("/permissions", response_model=s.PermissionCatalog)
def permissions(who: Principal = Depends(require_any("users.manage", "roles.manage", "agents.manage"))):
    return s.PermissionCatalog(permissions=rbac.PERMISSIONS, assistant_choices=rbac.ASSISTANT_PERMISSION_CHOICES)


@router.get("/roles", response_model=list[s.RoleOut])
def list_roles(request: Request, who: Principal = Depends(require_any("users.manage", "roles.manage"))):
    return [_role_out(r) for r in svc(request).store.list_roles(who.org_id, include_super=who.can("platform.manage"))]


def _check_role_body(body: s.RoleIn, who: Principal) -> list[str]:
    perms = sorted(set(body.permissions))
    unknown = [p for p in perms if p not in rbac.PERMISSIONS]
    if unknown:
        raise AppError("validation_error", f"Unknown permission(s): {', '.join(unknown)}", 422)
    if "platform.manage" in perms:
        raise AppError("validation_error", "'platform.manage' belongs to the built-in super admin role only.", 422)
    _forbid_escalation(who, perms)
    return perms


@router.post("/roles", response_model=s.RoleOut, status_code=201)
def create_role(body: s.RoleIn, request: Request, who: Principal = Depends(require_perm("roles.manage"))):
    perms = _check_role_body(body, who)
    return _role_out(svc(request).store.create_role(who.org_id, body.name, perms))


def _custom_role(sv, who: Principal, role_id: str) -> dict:
    r = sv.store.get_role(role_id, who.org_id)
    if not r or r["org_id"] != who.org_id:
        raise AppError("not_found", "Role not found.", 404)
    if r["builtin"]:
        raise AppError("forbidden", "Built-in roles cannot be changed.", 403)
    return r


@router.put("/roles/{role_id}", response_model=s.RoleOut)
def update_role(role_id: str, body: s.RoleIn, request: Request, who: Principal = Depends(require_perm("roles.manage"))):
    sv = svc(request)
    role = _custom_role(sv, who, role_id)
    perms = _check_role_body(body, who)
    _forbid_escalation(who, role["permissions"])  # cannot edit a role that is more powerful than you
    still = [u for u in sv.store.list_users(who.org_id) if u["active"] and "users.manage" in
             (perms if u["role_id"] == role_id else u["permissions"])]
    if not still and not who.can("platform.manage"):  # editing a role must not strip the last user manager
        raise AppError("conflict", "That would leave the organization without anyone who can manage users.", 409)
    return _role_out(sv.store.update_role(role_id, who.org_id, body.name, perms))


@router.delete("/roles/{role_id}", status_code=204)
def delete_role(role_id: str, request: Request, who: Principal = Depends(require_perm("roles.manage"))):
    sv = svc(request)
    _custom_role(sv, who, role_id)
    if any(u["role_id"] == role_id for u in sv.store.list_users(who.org_id)):
        raise AppError("conflict", "Some users still have this role. Move them to another role first.", 409)
    sv.store.delete_role(role_id, who.org_id)
    return Response(status_code=204)


def _keep_a_user_manager(sv, who: Principal, target: dict, *, active_after: bool, perms_after) -> None:
    """The organization must always keep at least one active user who can manage users (the platform admin
    can always step in, so they are exempt)."""
    if who.can("platform.manage"):
        return
    others = [u for u in sv.store.user_managers(who.org_id) if u["id"] != target["id"]]
    if not others and not (active_after and "users.manage" in perms_after):
        raise AppError("conflict", "That would leave the organization without anyone who can manage users.", 409)


def _resolve_role(sv, who: Principal, ref: str) -> dict:
    role = sv.store.get_role(ref, who.org_id) or sv.store.role_by_name(ref, who.org_id)
    if not role:
        raise AppError("validation_error", f"Unknown role '{ref}'.", 422)
    _forbid_escalation(who, role["permissions"])
    return role


def _org_user(sv, who: Principal, uid: str) -> dict:
    u = sv.store.get_user(uid)
    if not u or u["org_id"] != who.org_id:
        raise AppError("not_found", "User not found.", 404)
    return u


@router.get("/users", response_model=s.UserList)
def list_users(request: Request, who: Principal = Depends(require_perm("users.manage"))):
    return s.UserList(items=[_user_out(u) for u in svc(request).store.list_users(who.org_id)])


@router.post("/users", response_model=s.UserCreated, status_code=201)
def create_user(body: s.UserIn, request: Request, who: Principal = Depends(require_perm("users.manage"))):
    sv = svc(request)
    role = _resolve_role(sv, who, body.role)
    if not body.password and not body.api_key:
        raise AppError("validation_error", "Give the user a password, an API key, or both.", 422)
    email = pw_hash = None
    if body.password:
        if not body.email:
            raise AppError("validation_error", "An email address is needed so the user can sign in.", 422)
        check_password_policy(body.password, sv.config.min_password_length)
        pw_hash = hash_password(body.password)
    if body.email:
        email = normalize_email(body.email)
    user, token = sv.store.create_user(who.org_id, body.name, role["id"], email=email, password_hash=pw_hash,
                                       issue_token=body.api_key)
    return s.UserCreated(**_user_out(user).model_dump(), token=token)


@router.put("/users/{uid}", response_model=s.UserOut)
def update_user(uid: str, body: s.UserUpdate, request: Request, who: Principal = Depends(require_perm("users.manage"))):
    sv = svc(request)
    target = _org_user(sv, who, uid)
    _forbid_escalation(who, target["permissions"])  # cannot touch someone more powerful than you
    changes: dict = {}
    if body.name is not None:
        changes["name"] = body.name
    if body.role is not None:
        changes["role_id"] = _resolve_role(sv, who, body.role)["id"]
    if body.active is not None:
        changes["active"] = body.active
    if body.email is not None:
        changes["email"] = normalize_email(body.email)
    if body.password is not None:
        check_password_policy(body.password, sv.config.min_password_length)
        if not (body.email or target["email"]):
            raise AppError("validation_error", "Set an email address first so the user can sign in.", 422)
        changes["password_hash"] = hash_password(body.password)
    if uid == who.id and (changes.get("active") is False or "role_id" in changes):
        raise AppError("conflict", "You cannot deactivate or change the role of your own account.", 409)
    perms_after = (sv.store.get_role(changes["role_id"], who.org_id)["permissions"]
                   if "role_id" in changes else target["permissions"])
    _keep_a_user_manager(sv, who, target, active_after=changes.get("active", target["active"]), perms_after=perms_after)
    try:
        sv.store.update_user(uid, **changes)
    except sqlite3.IntegrityError as exc:
        raise AppError("conflict", "That name or email is already in use.", 409) from exc
    if body.password is not None or changes.get("active") is False:
        sv.store.delete_user_sessions(uid)  # takes effect immediately
    return _user_out(sv.store.get_user(uid))


@router.post("/users/{uid}/api-key", response_model=s.UserCreated)
def reset_api_key(uid: str, request: Request, who: Principal = Depends(require_perm("users.manage"))):
    sv = svc(request)
    target = _org_user(sv, who, uid)
    _forbid_escalation(who, target["permissions"])
    token = sv.store.reset_api_key(uid)
    return s.UserCreated(**_user_out(sv.store.get_user(uid)).model_dump(), token=token)


@router.delete("/users/{uid}", status_code=204)
def delete_user(uid: str, request: Request, who: Principal = Depends(require_perm("users.manage"))):
    sv = svc(request)
    target = _org_user(sv, who, uid)
    if uid == who.id:
        raise AppError("conflict", "You cannot delete your own account.", 409)
    _forbid_escalation(who, target["permissions"])
    _keep_a_user_manager(sv, who, target, active_after=False, perms_after=target["permissions"])
    sv.store.delete_user(uid)
    return Response(status_code=204)


# ======================================================================= tenants (platform)
def _org_out(o: dict) -> s.OrgOut:
    return s.OrgOut(**{k: o.get(k, 0) for k in s.OrgOut.model_fields})


@router.get("/orgs", response_model=s.OrgList)
def list_orgs(request: Request, who: Principal = Depends(require_perm("platform.manage"))):
    return s.OrgList(items=[_org_out(o) for o in svc(request).store.list_orgs()])


@router.post("/orgs", response_model=s.OrgOut, status_code=201)
def create_org(body: s.OrgIn, request: Request, who: Principal = Depends(require_perm("platform.manage"))):
    sv = svc(request)
    email = normalize_email(body.admin_email)
    check_password_policy(body.admin_password, sv.config.min_password_length)
    if sv.store.user_by_email(email):
        raise AppError("conflict", "That admin email address is already registered.", 409)
    org = sv.store.create_org(body.name, body.slug)
    sv.store.create_user(org["id"], body.admin_name, "admin", email=email,
                         password_hash=hash_password(body.admin_password), issue_token=False)
    for root in sv.connectors.allowed_roots():  # a private folder for this tenant's connectors
        (root / org["slug"]).mkdir(parents=True, exist_ok=True)
    return _org_out(next(o for o in sv.store.list_orgs() if o["id"] == org["id"]))


@router.put("/orgs/{org_id}", response_model=s.OrgOut)
def update_org(org_id: str, body: s.OrgUpdate, request: Request, who: Principal = Depends(require_perm("platform.manage"))):
    sv = svc(request)
    if not sv.store.get_org(org_id):
        raise AppError("not_found", "Organization not found.", 404)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    sv.store.update_org(org_id, **changes)
    if changes.get("active") is False:  # sign everyone in that organization out
        for u in sv.store.list_users(org_id):
            sv.store.delete_user_sessions(u["id"])
    return _org_out(next(o for o in sv.store.list_orgs() if o["id"] == org_id))


@router.get("/platform/settings", response_model=s.RuntimeSettings)
def platform_settings(request: Request, who: Principal = Depends(require_perm("platform.manage"))):
    return svc(request).db.get_settings(None)


@router.put("/platform/settings", response_model=s.RuntimeSettings)
def put_platform_settings(body: s.RuntimeSettings, request: Request,
                          who: Principal = Depends(require_perm("platform.manage"))):
    sv = svc(request)
    sv.db.save_settings(body, None)
    return sv.db.get_settings(None)


# ======================================================================= website embedding
def _base_url(request: Request) -> str:
    return (svc(request).config.public_url or str(request.base_url)).rstrip("/")


def snippet_for(request: Request, key: dict) -> str:
    base = _base_url(request)
    agent = f' data-agent="{key["agent_id"]}"' if key.get("agent_id") else ""
    return f'<script src="{base}/widget.js" data-key="{key["token"]}"{agent} async></script>'


def _embed_out(request: Request, k: dict) -> s.EmbedKeyOut:
    data = {f: k[f] for f in s.EmbedKeyOut.model_fields if f in k}
    return s.EmbedKeyOut(**{**data, "snippet": snippet_for(request, k), "wildcard": "*" in k["allowed_origins"]})


def _refresh_origins(request: Request) -> None:
    request.app.state.embed_origins = svc(request).store.all_embed_origins()  # used by the CORS middleware


def _check_embed(sv, who: Principal, origins: list[str] | None, agent_id: str | None) -> list[str] | None:
    clean = None
    if origins is not None:
        clean = []
        for o in origins:
            if not valid_origin_pattern(o):
                raise AppError("validation_error", f"'{o}' is not a valid origin. Use https://example.com, "
                                                   "https://*.example.com, or * for any website.", 422)
            n = normalize_origin(o)
            if n not in clean:
                clean.append(n)
        if not clean:
            raise AppError("validation_error", "Add at least one website that may embed the chat (or * for any).", 422)
    if agent_id:
        agent = sv.store.get_agent(who.org_id, agent_id)
        if not agent:
            raise AppError("not_found", "Assistant not found.", 404)
        if agent["required_permission"] not in rbac.VISITOR_PERMISSIONS:
            raise AppError("validation_error", "That assistant needs permissions anonymous website visitors "
                                               "do not have, so it cannot be embedded publicly.", 422)
    return clean


@router.get("/embed-keys", response_model=s.EmbedKeyList)
def list_embed_keys(request: Request, who: Principal = Depends(require_perm("embed.manage"))):
    return s.EmbedKeyList(items=[_embed_out(request, k) for k in svc(request).store.list_embed_keys(who.org_id)])


@router.post("/embed-keys", response_model=s.EmbedKeyOut, status_code=201)
def create_embed_key(body: s.EmbedKeyIn, request: Request, who: Principal = Depends(require_perm("embed.manage"))):
    sv = svc(request)
    origins = _check_embed(sv, who, body.allowed_origins, body.agent_id) if body.allowed_origins else None
    if origins is None:
        raise AppError("validation_error", "Add at least one website that may embed the chat (or * for any).", 422)
    key, _ = sv.store.create_embed_key(who.org_id, body.name, origins, body.agent_id, body.title, body.welcome, body.color)
    _refresh_origins(request)
    return _embed_out(request, key)


@router.put("/embed-keys/{kid}", response_model=s.EmbedKeyOut)
def update_embed_key(kid: str, body: s.EmbedKeyUpdate, request: Request,
                     who: Principal = Depends(require_perm("embed.manage"))):
    sv = svc(request)
    if not sv.store.get_embed_key(kid, who.org_id):
        raise AppError("not_found", "Embed key not found.", 404)
    changes = {k: v for k, v in body.model_dump(exclude={"clear_agent"}).items() if v is not None}
    origins = _check_embed(sv, who, changes.get("allowed_origins"), changes.get("agent_id"))
    if origins is not None:
        changes["allowed_origins"] = origins
    if body.clear_agent:
        changes["agent_id"] = None
    if changes:
        sv.store.update_embed_key(kid, who.org_id, **changes)
    _refresh_origins(request)
    return _embed_out(request, sv.store.get_embed_key(kid, who.org_id))


@router.delete("/embed-keys/{kid}", status_code=204)
def delete_embed_key(kid: str, request: Request, who: Principal = Depends(require_perm("embed.manage"))):
    if not svc(request).store.delete_embed_key(kid, who.org_id):
        raise AppError("not_found", "Embed key not found.", 404)
    _refresh_origins(request)
    return Response(status_code=204)


@router.get("/embed/config", response_model=s.EmbedConfig)
def embed_config(request: Request):
    """Look and wording of the widget; called by widget.js with its embed key."""
    who = principal_for(request)
    key = svc(request).store.get_embed_key(who.embed_key_id) if who.kind == "visitor" else None
    if not key:
        raise AppError("not_found", "This is not an embed key.", 404)
    return s.EmbedConfig(title=key["title"], welcome=key["welcome"], color=key["color"], agent_id=key["agent_id"])
