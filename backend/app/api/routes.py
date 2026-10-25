import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.auth import Principal, principal_for, require_perm
from app.api.limits import throttle
from app.core.errors import AppError, OllamaError, envelope
from app.models import schemas as s
from app.services.agent_tools import ToolContext, describe, execute_confirmed
from app.services.ollama_service import model_matches

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
EMBED_KEY_CHAT_PER_MINUTE = 600


def svc(request: Request):
    return request.app.state.services


def doc_out(row: dict) -> s.DocumentOut:
    return s.DocumentOut(**{k: row[k] for k in s.DocumentOut.model_fields if k in row})


def msg_out(row: dict, store=None) -> s.MessageOut:
    data = dict(row)
    if store is not None and data.get("role") == "assistant":
        data["feedback"] = store.get_feedback(data["id"])
        tools = []
        for t in data.get("tools") or []:  # show the live state (confirmed / rejected / expired) of pending actions
            res = t.get("result") if isinstance(t.get("result"), dict) else {}
            action = store.get_action(res["action_id"]) if res.get("action_id") else None
            if action:
                status = "awaiting_user_confirmation" if action["status"] == "pending" else action["status"]
                t = {**t, "result": {**res, "status": status}}
            tools.append(t)
        data["tools"] = tools
    return s.MessageOut(**data)


def session_out(sv, row: dict) -> s.SessionOut:
    h = sv.store.open_handoff(row["id"])
    return s.SessionOut(**{k: row.get(k) for k in s.SessionOut.model_fields if k != "handoff_status"},
                        handoff_status=h["status"] if h else None)


def owned_session(sv, who: Principal, session_id: str) -> dict:
    """The session if it is in the caller's organisation and either theirs or readable by support staff.

    Anything else is a 404, never a 403, so nobody can probe which ids exist in other organisations.
    """
    row = sv.db.get_session(session_id)
    if not row or row.get("org_id") != who.org_id:
        raise AppError("not_found", "Session not found.", 404)
    if row["owner"] != who.id and not who.can("handoffs.handle"):
        raise AppError("not_found", "Session not found.", 404)
    return row


def org_document(sv, who: Principal, document_id: str) -> dict:
    row = sv.db.get_document(document_id)
    if not row or row.get("org_id") != who.org_id:
        raise AppError("not_found", "Document not found.", 404)
    return row


# -- health / models -----------------------------------------------------------
async def _ollama_status(sv, org_id: str) -> s.OllamaStatus:
    cfg = sv.db.get_settings(org_id)
    st = s.OllamaStatus(
        reachable=False, url=cfg.ollama_url,
        chat_model=cfg.chat_model, embedding_model=cfg.embedding_model,
    )
    try:
        installed = [m["name"] for m in await sv.ollama.list_models()]
    except OllamaError as exc:
        st.error = exc.message
        return st
    st.reachable = True
    st.version = await sv.ollama.version()
    st.chat_model_available = any(model_matches(cfg.chat_model, i) for i in installed)
    st.embedding_model_available = any(model_matches(cfg.embedding_model, i) for i in installed)
    missing = [
        m for m, ok in ((cfg.chat_model, st.chat_model_available),
                        (cfg.embedding_model, st.embedding_model_available)) if not ok
    ]
    if missing:
        st.error = "Missing model(s): " + ", ".join(f"ollama pull {m}" for m in missing)
    return st


@router.get("/health", response_model=s.HealthResponse)
async def health(request: Request):
    sv = svc(request)
    who = None
    try:
        who = principal_for(request)
    except AppError:
        pass
    st = await _ollama_status(sv, who.org_id if who else "default")
    ok = st.reachable and st.chat_model_available and st.embedding_model_available
    if who is None or who.kind == "visitor":  # anonymous callers learn the status only, not internal URLs / model names
        st = s.OllamaStatus(reachable=st.reachable, url="", chat_model="", embedding_model="",
                            chat_model_available=st.chat_model_available,
                            embedding_model_available=st.embedding_model_available)
    elif not who.can("platform.manage"):
        st.url = ""
    return s.HealthResponse(status="ok" if ok else "degraded", ollama=st)


@router.get("/models", response_model=s.ModelsResponse)
async def models(request: Request, who: Principal = Depends(require_perm("settings.write"))):
    return s.ModelsResponse(models=[s.ModelInfo(**m) for m in await svc(request).ollama.list_models()])


# -- settings (per organisation; the Ollama URL belongs to the platform) -------------------
HIDDEN_URL = "http://managed-by-platform.invalid"


@router.get("/settings", response_model=s.SettingsResponse)
async def get_settings(request: Request, who: Principal = Depends(require_perm("settings.read"))):
    return await _settings_response(svc(request), who)


@router.put("/settings", response_model=s.SettingsResponse)
async def put_settings(body: s.RuntimeSettings, request: Request,
                       who: Principal = Depends(require_perm("settings.write"))):
    sv = svc(request)
    # Worst case: top_k full-size chunks plus room for instructions, history and the question.
    needed = body.top_k * (body.chunk_size // 3 + 40)
    room = body.num_ctx - body.max_tokens - 1500
    if needed > room:
        raise AppError(
            "validation_error",
            f"top_k x chunk_size could need ~{needed} tokens of context but only ~{max(room, 0)} fit in "
            f"num_ctx={body.num_ctx} after reserving {body.max_tokens} answer tokens. Lower top_k or chunk_size, "
            "or raise the context window.", 422)
    platform = sv.db.get_settings(None)
    if who.can("platform.manage") and body.ollama_url != platform.ollama_url:
        sv.db.save_settings(platform.model_copy(update={"ollama_url": body.ollama_url}), None)
    sv.db.save_settings(body, who.org_id)  # the URL part is never stored per organisation
    return await _settings_response(sv, who)


async def _settings_response(sv, who: Principal) -> s.SettingsResponse:
    cfg = sv.db.get_settings(who.org_id)
    st = await _ollama_status(sv, who.org_id)
    msgs = []
    if not st.reachable:
        msgs.append(st.error or "Ollama is unreachable.")
    else:
        if not st.chat_model_available:
            msgs.append(f"Chat model '{cfg.chat_model}' is not installed. Run: ollama pull {cfg.chat_model}")
        if not st.embedding_model_available:
            msgs.append(f"Embedding model '{cfg.embedding_model}' is not installed. Run: ollama pull {cfg.embedding_model}")
    stale = sum(1 for d in sv.db.ready_documents(None, who.org_id) if d["embedding_model"] != cfg.embedding_model)
    if stale:
        msgs.append(f"{stale} document(s) were indexed with another embedding model; re-upload them to search with '{cfg.embedding_model}'.")
    shown = cfg if who.can("platform.manage") else cfg.model_copy(update={"ollama_url": HIDDEN_URL})
    return s.SettingsResponse(
        settings=shown,
        validation=s.SettingsValidation(
            ollama_reachable=st.reachable,
            chat_model_available=st.chat_model_available,
            embedding_model_available=st.embedding_model_available,
            messages=msgs,
        ),
        reindex_required=stale > 0,
        stale_documents=stale,
    )


# -- documents ---------------------------------------------------------------
@router.post("/documents/upload", response_model=s.UploadResponse, status_code=202)
async def upload(request: Request, response: Response, files: list[UploadFile] = File(...),
                 who: Principal = Depends(require_perm("documents.write"))):
    sv = svc(request)
    throttle(sv.limits, f"upload:{who.org_id}:{who.id}", sv.config.rate_limit_per_minute)
    results = []
    for f in files:
        name = f.filename or "unnamed"
        try:
            doc, dup = await sv.ingestion.accept(f, who.org_id)
            results.append(s.UploadResult(filename=name, document=doc_out(doc), duplicate=dup))
        except AppError as exc:
            results.append(s.UploadResult(filename=name, error=exc.message))
    if all(r.error for r in results):
        response.status_code = 400
    return s.UploadResponse(results=results)


@router.get("/documents", response_model=s.DocumentList)
def list_documents(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   who: Principal = Depends(require_perm("documents.read"))):
    rows, total = svc(request).db.list_documents(limit, offset, who.org_id)
    return s.DocumentList(items=[doc_out(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/documents/{document_id}", response_model=s.DocumentOut)
def get_document(document_id: str, request: Request, who: Principal = Depends(require_perm("documents.read"))):
    return doc_out(org_document(svc(request), who, document_id))


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: str, request: Request,
                          who: Principal = Depends(require_perm("documents.write"))):
    sv = svc(request)
    org_document(sv, who, document_id)
    await sv.ingestion.delete(document_id)
    return Response(status_code=204)


# -- sessions ----------------------------------------------------------------
@router.get("/sessions", response_model=s.SessionList)
def list_sessions(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  who: Principal = Depends(require_perm("chat.use"))):
    sv = svc(request)
    rows, total = sv.db.list_sessions(limit, offset, owner=who.id, org_id=who.org_id)
    return s.SessionList(items=[session_out(sv, r) for r in rows], total=total, limit=limit, offset=offset)


def check_agent(sv, agent_id: str | None, who: Principal) -> str | None:
    """Which assistant a new session may use. Embedded-widget visitors are pinned to the key's assistant."""
    if who.kind == "visitor":
        return who.embed_agent
    if not agent_id:
        return None
    agent = sv.store.get_agent(who.org_id, agent_id)
    if not agent:
        raise AppError("not_found", "Agent not found.", 404)
    if agent["required_permission"] not in who.permissions:
        raise AppError("forbidden", f"The '{agent['name']}' assistant needs the "
                                    f"'{agent['required_permission']}' permission.", 403)
    return agent_id


@router.post("/sessions", response_model=s.SessionOut, status_code=201)
def create_session(request: Request, body: s.SessionCreate | None = None,
                   who: Principal = Depends(require_perm("chat.use"))):
    sv = svc(request)
    body = body or s.SessionCreate()
    row = sv.db.create_session(body.title, check_agent(sv, body.agent_id, who), who.id, who.org_id)
    return session_out(sv, row)


@router.get("/sessions/{session_id}/messages", response_model=s.MessageList)
def session_messages(session_id: str, request: Request, who: Principal = Depends(require_perm("chat.use"))):
    sv = svc(request)
    owned_session(sv, who, session_id)
    h = sv.store.open_handoff(session_id)
    return s.MessageList(items=[msg_out(m, sv.store) for m in sv.db.list_messages(session_id)],
                         handoff_status=h["status"] if h else None)


@router.delete("/sessions/{session_id}/messages", status_code=204)
def clear_session(session_id: str, request: Request, who: Principal = Depends(require_perm("chat.use"))):
    sv = svc(request)
    owned_session(sv, who, session_id)
    sv.db.clear_messages(session_id)
    return Response(status_code=204)


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, request: Request, who: Principal = Depends(require_perm("chat.use"))):
    sv = svc(request)
    owned_session(sv, who, session_id)
    sv.store.close_session_records(session_id)  # no dangling handoffs or pending actions
    sv.db.delete_session(session_id)
    return Response(status_code=204)


# -- chat --------------------------------------------------------------------
def _prepare_session(sv, body: s.ChatRequest, who: Principal) -> str:
    if body.session_id:
        owned_session(sv, who, body.session_id)
        return body.session_id
    return sv.db.create_session("New chat", check_agent(sv, body.agent_id, who), who.id, who.org_id)["id"]


def _throttle_chat(sv, who: Principal) -> None:
    throttle(sv.limits, f"chat:{who.org_id}:{who.id}", sv.config.rate_limit_per_minute)
    if who.kind == "visitor":  # one popular site must not starve the model for everybody
        throttle(sv.limits, f"embedkey:{who.embed_key_id}", EMBED_KEY_CHAT_PER_MINUTE)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/chat", response_model=s.ChatResponse)
async def chat(body: s.ChatRequest, request: Request, who: Principal = Depends(require_perm("chat.use"))):
    sv = svc(request)
    _throttle_chat(sv, who)
    sid = _prepare_session(sv, body, who)
    warnings: list[str] = []
    final = None
    async for ev in sv.rag.stream_answer(body.question, sid, body.document_ids, who):
        if ev["type"] == "sources":
            warnings = ev["warnings"]
        elif ev["type"] == "done":
            final = ev["message"]
    return s.ChatResponse(session_id=sid, message=msg_out(final), warnings=warnings)


@router.post("/chat/stream")
async def chat_stream(body: s.ChatRequest, request: Request, who: Principal = Depends(require_perm("chat.use"))):
    """SSE events: session, handoff, sources, token, tool, done, error."""
    sv = svc(request)
    _throttle_chat(sv, who)
    sid = _prepare_session(sv, body, who)

    async def gen() -> AsyncIterator[str]:
        yield _sse("session", {"session_id": sid})
        try:
            async for ev in sv.rag.stream_answer(body.question, sid, body.document_ids, who):
                kind = ev["type"]
                if kind == "sources":
                    yield _sse("sources", {"citations": ev["citations"], "warnings": ev["warnings"]})
                elif kind == "token":
                    yield _sse("token", {"text": ev["text"]})
                elif kind == "tool":
                    yield _sse("tool", {k: ev[k] for k in ("name", "args", "ok", "result")})
                elif kind == "handoff":
                    yield _sse("handoff", {"status": ev["status"]})
                else:
                    yield _sse("done", {"message": msg_out(ev["message"], sv.store).model_dump()})
        except AppError as exc:
            yield _sse("error", {"code": exc.code, "message": exc.message})
        except Exception:
            log.exception("chat stream failed")
            yield _sse("error", {"code": "internal_error", "message": "Unexpected server error."})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# -- user-confirmed actions -----------------------------------------------------
def _own_action(sv, who: Principal, action_id: str) -> dict:
    a = sv.store.get_action(action_id)
    session = sv.db.get_session(a["session_id"]) if a else None
    if not a or not session or a["owner"] != who.id or session.get("org_id") != who.org_id:
        raise AppError("not_found", "Action not found.", 404)  # also hides other users' / organisations' actions
    if a["status"] != "pending":
        raise AppError("conflict", f"This action is already {a['status']}.", 409)
    return a


@router.post("/actions/{action_id}/confirm", response_model=s.ActionOutcome)
async def confirm_action(action_id: str, request: Request, who: Principal = Depends(require_perm("chat.use"))):
    """Run an action the assistant proposed, after the user approved it. Re-validated at execution time."""
    sv = svc(request)
    a = _own_action(sv, who, action_id)
    ctx = ToolContext(sv.store, who.id, a["session_id"], who.org_id, who.permissions)
    ok, result = await asyncio.to_thread(execute_confirmed, ctx, a["tool"], a["args"])
    status = "confirmed" if ok else "failed"
    if not sv.store.finish_action(action_id, status, result):
        raise AppError("conflict", "This action was already handled.", 409)  # lost a double-click race
    text = (f"Done: {describe(a['tool'], a['args'])}." if ok
            else f"I could not complete that: {result.get('error', 'unknown error')}.")
    msg = sv.db.add_message(a["session_id"], "assistant", text, meta={"tools": [
        {"name": a["tool"], "args": a["args"], "ok": ok, "result": {**result, "action_id": action_id, "status": status}}]})
    return s.ActionOutcome(status=status, ok=ok, result=result, message=msg_out(msg, sv.store))


@router.post("/actions/{action_id}/reject", response_model=s.ActionOutcome)
def reject_action(action_id: str, request: Request, who: Principal = Depends(require_perm("chat.use"))):
    sv = svc(request)
    a = _own_action(sv, who, action_id)
    if not sv.store.finish_action(action_id, "rejected", None):
        raise AppError("conflict", "This action was already handled.", 409)
    msg = sv.db.add_message(a["session_id"], "assistant", "Okay, I cancelled that.")
    return s.ActionOutcome(status="rejected", ok=False, result={}, message=msg_out(msg, sv.store))


# -- error handlers ----------------------------------------------------------
def register_handlers(app) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(envelope(exc.code, exc.message), status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)  # also covers unknown routes (404) and 405
    async def _http(_: Request, exc: StarletteHTTPException):
        return JSONResponse(envelope("http_error", str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        msg = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors()
        )
        return JSONResponse(envelope("validation_error", msg), status_code=422)

    @app.exception_handler(ValidationError)
    async def _pyd(_: Request, exc: ValidationError):
        return JSONResponse(envelope("validation_error", str(exc)), status_code=422)
