"""Sign in / sign out / who am I / change password."""
from fastapi import APIRouter, Depends, Request, Response

from app.api.auth import Principal, authenticated
from app.api.routes import svc
from app.core.errors import AppError
from app.core.security import check_password_policy, hash_password, verify_password
from app.models import schemas as s

router = APIRouter(prefix="/api/auth")


def me_out(sv, who: Principal) -> s.Me:
    org = sv.store.get_org(who.org_id) or {"name": who.org_id}
    return s.Me(id=who.id, name=who.name, email=who.email, org_id=who.org_id, org_name=org["name"], role=who.role,
                permissions=sorted(who.permissions), is_super=who.is_super, kind=who.kind,
                auth_enabled=sv.config.auth_enabled)


@router.get("/me", response_model=s.Me)
def me(request: Request, who: Principal = Depends(authenticated)):
    return me_out(svc(request), who)


@router.post("/login", response_model=s.LoginOut)
def login(body: s.LoginIn, request: Request):
    sv = svc(request)
    cfg = sv.config
    if not cfg.auth_enabled:
        raise AppError("auth_disabled", "Sign-in is not enabled on this server (AUTH_ENABLED=false).", 400)
    email = body.email.strip().lower()
    ip = request.client.host if request.client else "unknown"
    ip_key, email_key = f"authfail:{ip}", f"loginfail:{email}"
    if sv.limits.over(ip_key, cfg.auth_failures_per_minute) or \
            sv.limits.over(email_key, cfg.login_failures_per_15min, window=900.0):
        raise AppError("rate_limited", "Too many failed sign-in attempts. Try again in a few minutes.", 429)
    user = sv.store.user_by_email(email)
    stored = sv.store.password_hash_of(user["id"]) if user and user["active"] else None
    ok = verify_password(body.password, stored)  # always does the hashing work, so timing does not reveal accounts
    if not (ok and user):
        sv.limits.record(ip_key)
        sv.limits.record(email_key)
        raise AppError("invalid_credentials", "Incorrect email or password.", 401)
    perms = frozenset(user["permissions"])
    if not user["org_active"] and "platform.manage" not in perms:
        raise AppError("org_disabled", "This organization has been disabled.", 403)
    token, expires = sv.store.create_auth_session(user["id"], cfg.session_hours)
    who = Principal(user["id"], user["name"], user["org_id"], user["role_name"], perms, email=user["email"])
    return s.LoginOut(token=token, expires_at=expires, me=me_out(sv, who))


@router.post("/logout", status_code=204)
def logout(request: Request, who: Principal = Depends(authenticated)):
    if who.token:
        svc(request).store.delete_auth_session(who.token)
    return Response(status_code=204)


@router.post("/password", status_code=204)
def change_password(body: s.PasswordChange, request: Request, who: Principal = Depends(authenticated)):
    sv = svc(request)
    if who.kind != "user":
        raise AppError("forbidden", "This account has no password.", 403)
    stored = sv.store.password_hash_of(who.id)
    if not verify_password(body.current_password, stored):
        raise AppError("invalid_credentials", "Your current password is incorrect.", 401)
    check_password_policy(body.new_password, sv.config.min_password_length)
    sv.store.update_user(who.id, password_hash=hash_password(body.new_password))
    sv.store.delete_user_sessions(who.id, except_token=who.token)  # sign out everywhere else
    return Response(status_code=204)
