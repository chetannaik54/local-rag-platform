"""Assistants, human handoff, connectors, analytics, feedback and voice. Everything is scoped to the caller's
organisation, and every endpoint names the permission it needs."""
import asyncio

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile

from app.api.auth import Principal, require_perm
from app.api.limits import throttle
from app.api.routes import owned_session, svc
from app.core import rbac
from app.core.errors import AppError
from app.models import schemas as s
from app.services.agent_tools import TOOLS

router = APIRouter(prefix="/api")
MAX_AUDIO_BYTES = 25 * 1024 * 1024


def _agent_out(a: dict) -> s.AgentOut:
    return s.AgentOut(**{k: a[k] for k in s.AgentOut.model_fields})


# -- assistants ------------------------------------------------------------------
@router.get("/agents", response_model=s.AgentList)
def list_agents(request: Request, who: Principal = Depends(require_perm("chat.use"))):
    manage = who.can("agents.manage")
    items = []
    for a in svc(request).store.list_agents(who.org_id):
        if a["required_permission"] not in who.permissions:
            continue  # assistants above your permissions are not even listed
        if who.kind == "visitor" and a["id"] != (who.embed_agent or "general"):
            continue  # an embedded widget only ever sees its own assistant
        out = _agent_out(a)
        if not manage:  # prompts, tools and document scopes are configuration, not for end users
            out = out.model_copy(update={"system_prompt": "", "document_ids": []})
        items.append(out)
    return s.AgentList(
        items=items,
        available_tools=[s.ToolInfo(name=n, description=spec["function"]["description"])
                         for n, (spec, _) in TOOLS.items()] if manage else [],
    )


def _validate_agent(sv, who: Principal, body: s.AgentIn) -> None:
    bad = [t for t in body.tools if t not in TOOLS]
    if bad:
        raise AppError("validation_error", f"Unknown tool(s): {', '.join(bad)}", 422)
    known = {d["id"] for d in sv.db.ready_documents(None, who.org_id)}
    if any(d not in known for d in body.document_ids):
        raise AppError("validation_error", "document_ids contains unknown or unfinished documents.", 422)
    if body.required_permission not in rbac.ASSISTANT_PERMISSION_CHOICES:
        raise AppError("validation_error", "required_permission must be one of: "
                       + ", ".join(rbac.ASSISTANT_PERMISSION_CHOICES), 422)
    if body.required_permission not in who.permissions:  # you cannot hand out access you do not have
        raise AppError("forbidden", "You cannot require a permission you do not hold yourself.", 403)


@router.post("/agents", response_model=s.AgentOut, status_code=201)
def create_agent(body: s.AgentIn, request: Request, who: Principal = Depends(require_perm("agents.manage"))):
    sv = svc(request)
    _validate_agent(sv, who, body)
    return _agent_out(sv.store.save_agent(who.org_id, None, **body.model_dump()))


@router.put("/agents/{agent_id}", response_model=s.AgentOut)
def update_agent(agent_id: str, body: s.AgentIn, request: Request,
                 who: Principal = Depends(require_perm("agents.manage"))):
    sv = svc(request)
    if not sv.store.get_agent(who.org_id, agent_id):
        raise AppError("not_found", "Agent not found.", 404)
    _validate_agent(sv, who, body)
    return _agent_out(sv.store.save_agent(who.org_id, agent_id, **body.model_dump()))


@router.delete("/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: str, request: Request, who: Principal = Depends(require_perm("agents.manage"))):
    if not svc(request).store.delete_agent(who.org_id, agent_id):
        raise AppError("not_found", "Agent not found, or it is built in and cannot be deleted.", 404)
    return Response(status_code=204)


# -- human handoff -------------------------------------------------------------
@router.post("/sessions/{session_id}/handoff", response_model=s.HandoffOut, status_code=201)
def request_handoff(session_id: str, request: Request, body: s.HandoffRequest | None = None,
                    who: Principal = Depends(require_perm("chat.use"))):
    sv = svc(request)
    owned_session(sv, who, session_id)
    throttle(sv.limits, f"handoff:{who.org_id}:{who.id}", 10)
    already = sv.store.open_handoff(session_id)
    h = sv.store.create_handoff(session_id, (body.reason if body else "") or "Requested by user")
    if not already:
        sv.db.add_message(session_id, "assistant",
                          "I've asked a human agent to join this conversation. Please wait a moment.",
                          meta={"handoff": True})
    return s.HandoffOut(**h)


@router.delete("/sessions/{session_id}/handoff", status_code=204)
def cancel_handoff(session_id: str, request: Request, who: Principal = Depends(require_perm("chat.use"))):
    sv = svc(request)
    owned_session(sv, who, session_id)
    h = sv.store.open_handoff(session_id)
    if h and sv.store.resolve_handoff(h["id"]):
        sv.db.add_message(session_id, "assistant", "You're back with the assistant. How can I help?")
    return Response(status_code=204)


@router.get("/handoffs", response_model=s.HandoffList)
def list_handoffs(request: Request, include_resolved: bool = Query(False),
                  who: Principal = Depends(require_perm("handoffs.handle"))):
    return s.HandoffList(items=[s.HandoffOut(**h)
                                for h in svc(request).store.list_handoffs(who.org_id, not include_resolved)])


def _handoff(sv, who: Principal, hid: str) -> dict:
    h = sv.store.get_handoff(hid, who.org_id)
    if not h:
        raise AppError("not_found", "Handoff not found.", 404)
    return h


@router.post("/handoffs/{hid}/claim", response_model=s.HandoffOut)
def claim_handoff(hid: str, request: Request, who: Principal = Depends(require_perm("handoffs.handle"))):
    sv = svc(request)
    _handoff(sv, who, hid)
    if not sv.store.claim_handoff(hid, who.id):
        raise AppError("conflict", "This conversation was already claimed or is resolved.", 409)
    return s.HandoffOut(**_handoff(sv, who, hid))


@router.post("/handoffs/paraphrase", response_model=s.ParaphraseOut)
async def paraphrase(body: s.ParaphraseIn, request: Request,
                     who: Principal = Depends(require_perm("handoffs.handle"))):
    sv = svc(request)
    cfg = sv.db.get_settings(who.org_id)
    await sv.ollama.ensure_models(cfg.chat_model)
    text = await sv.ollama.chat(
        cfg.chat_model,
        [{"role": "system", "content": (
            f"Rewrite the support reply below in a {body.tone} tone. Keep the meaning, facts and language "
            "unchanged; do not add new promises. Output only the rewritten reply.")},
         {"role": "user", "content": body.text}],
        {"temperature": 0.3, "num_ctx": cfg.num_ctx, "num_predict": cfg.max_tokens},
    )
    return s.ParaphraseOut(text=text.strip() or body.text)


@router.post("/handoffs/{hid}/reply", response_model=s.MessageOut, status_code=201)
def reply_handoff(hid: str, body: s.ReplyIn, request: Request,
                  who: Principal = Depends(require_perm("handoffs.handle"))):
    sv = svc(request)
    h = _handoff(sv, who, hid)
    if h["status"] == "resolved":
        raise AppError("conflict", "This conversation is resolved.", 409)
    if not sv.db.get_session(h["session_id"]):  # the customer deleted the chat
        sv.store.resolve_handoff(hid)
        raise AppError("not_found", "The customer has closed this conversation.", 404)
    if h["status"] == "waiting":
        sv.store.claim_handoff(hid, who.id)
    elif h["assignee"] != who.id and not who.can("users.manage"):  # admins may take over a colleague's chat
        raise AppError("conflict", "This conversation is being handled by a colleague.", 409)
    return s.MessageOut(**sv.db.add_message(h["session_id"], "human", body.text.strip()))


@router.post("/handoffs/{hid}/resolve", response_model=s.HandoffOut)
def resolve_handoff(hid: str, request: Request, who: Principal = Depends(require_perm("handoffs.handle"))):
    sv = svc(request)
    h = _handoff(sv, who, hid)
    if h["status"] != "resolved" and not sv.store.resolve_handoff(hid):
        raise AppError("conflict", "Could not resolve this handoff.", 409)
    if h["status"] != "resolved":
        sv.db.add_message(h["session_id"], "assistant",
                          "The human agent has finished. You're back with the assistant.")
    return s.HandoffOut(**_handoff(sv, who, hid))


@router.get("/canned", response_model=s.CannedList)
def list_canned(request: Request, who: Principal = Depends(require_perm("handoffs.handle"))):
    return s.CannedList(items=[s.CannedOut(**c) for c in svc(request).store.list_canned(who.org_id)])


@router.post("/canned", response_model=s.CannedOut, status_code=201)
def add_canned(body: s.CannedIn, request: Request, who: Principal = Depends(require_perm("handoffs.handle"))):
    return s.CannedOut(**svc(request).store.add_canned(who.org_id, body.title, body.text))


@router.delete("/canned/{cid}", status_code=204)
def delete_canned(cid: str, request: Request, who: Principal = Depends(require_perm("handoffs.handle"))):
    if not svc(request).store.delete_canned(who.org_id, cid):
        raise AppError("not_found", "Canned response not found.", 404)
    return Response(status_code=204)


# -- connectors ---------------------------------------------------------------
def _connector(sv, who: Principal, cid: str) -> dict:
    c = sv.store.get_connector(cid, who.org_id)
    if not c:
        raise AppError("not_found", "Connector not found.", 404)
    return c


def _org_slug(sv, who: Principal) -> str:
    return (sv.store.get_org(who.org_id) or {"slug": "default"})["slug"]


@router.get("/connectors", response_model=s.ConnectorList)
def list_connectors(request: Request, who: Principal = Depends(require_perm("connectors.manage"))):
    sv = svc(request)
    roots = sv.connectors.org_roots(_org_slug(sv, who), who.can("platform.manage"))
    return s.ConnectorList(items=[s.ConnectorOut(**c) for c in sv.store.list_connectors(who.org_id)],
                           allowed_roots=[str(r) for r in roots])


def _spawn(request: Request, coro) -> None:
    tasks = request.app.state.tasks
    t = asyncio.create_task(coro)
    tasks.add(t)
    t.add_done_callback(tasks.discard)


@router.post("/connectors", response_model=s.ConnectorOut, status_code=201)
async def add_connector(body: s.ConnectorIn, request: Request,
                        who: Principal = Depends(require_perm("connectors.manage"))):
    sv = svc(request)
    ref = await asyncio.to_thread(sv.connectors.validate, body.type, body.ref, _org_slug(sv, who),
                                  who.can("platform.manage"))
    c = sv.store.add_connector(who.org_id, body.type, ref, body.interval_minutes)
    c["enabled"] = True
    _spawn(request, sv.connectors.sync(c["id"]))
    return s.ConnectorOut(**c)


@router.post("/connectors/{cid}/sync", status_code=202)
async def sync_connector(cid: str, request: Request, who: Principal = Depends(require_perm("connectors.manage"))):
    sv = svc(request)
    _connector(sv, who, cid)
    _spawn(request, sv.connectors.sync(cid))
    return {"status": "started"}


@router.delete("/connectors/{cid}", status_code=204)
async def delete_connector(cid: str, request: Request, delete_documents: bool = Query(True),
                           who: Principal = Depends(require_perm("connectors.manage"))):
    sv = svc(request)
    _connector(sv, who, cid)
    if delete_documents:
        for d in sv.store.connector_documents(cid):
            await sv.ingestion.delete(d["id"])
    sv.store.delete_connector(cid)
    return Response(status_code=204)


# -- analytics / feedback ---------------------------------------------------------
@router.get("/analytics")
def analytics(request: Request, days: int = Query(30, ge=1, le=365),
              who: Principal = Depends(require_perm("analytics.view"))):
    return svc(request).store.analytics(who.org_id, days)


@router.post("/messages/{message_id}/feedback", status_code=204)
def feedback(message_id: str, body: s.FeedbackIn, request: Request,
             who: Principal = Depends(require_perm("chat.use"))):
    sv = svc(request)
    m = sv.db.one("SELECT session_id, role FROM messages WHERE id=?", (message_id,))
    if not m or m["role"] != "assistant":
        raise AppError("not_found", "Message not found.", 404)
    owned_session(sv, who, m["session_id"])
    sv.store.set_feedback(message_id, body.rating, body.comment)
    return Response(status_code=204)


# -- voice ----------------------------------------------------------------------
@router.get("/voice/status")
def voice_status(request: Request, who: Principal = Depends(require_perm("chat.use"))):
    v = svc(request).voice
    return {"stt_available": v.available() and who.kind != "visitor", "model": svc(request).config.whisper_model}


@router.post("/voice/transcribe", response_model=s.TranscriptionOut)
async def transcribe(request: Request, audio: UploadFile = File(...), language: str | None = Query(None, max_length=8),
                     who: Principal = Depends(require_perm("chat.use"))):
    if who.kind == "visitor":
        raise AppError("forbidden", "Voice input is not available in embedded widgets.", 403)
    throttle(svc(request).limits, f"voice:{who.org_id}:{who.id}", svc(request).config.rate_limit_per_minute)
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if not data:
        raise AppError("empty_file", "No audio received.", 422)
    if len(data) > MAX_AUDIO_BYTES:
        raise AppError("file_too_large", "Audio is larger than 25 MB.", 413)
    suffix = "." + (audio.filename or "audio.webm").rsplit(".", 1)[-1][:5]
    text, lang = await svc(request).voice.transcribe(data, suffix, language)
    return s.TranscriptionOut(text=text, language=lang)
