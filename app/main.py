"""Funcional Lumos: servidor web, rotas da API e sincronização em segundo plano."""
import base64
import io
import json
import os
import secrets
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import ai, mail
from .db import (Analysis, Decision, Document, Email, SessionLocal, get_setting,
                 init_db)
from .mail import index_email, normalize_subject, strip_quoted
from .retrieval import mark_dirty

APP_USER = os.getenv("APP_USER", "startgi")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "5"))
AUTO_ANALYZE = os.getenv("AUTO_ANALYZE", "true").lower() == "true"
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Funcional Lumos")
state = {"last_sync": None, "last_error": None, "running": False, "log": []}


def log(msg):
    line = f"{datetime.now():%d/%m %H:%M:%S} {msg}"
    print(line, flush=True)
    state["log"] = (state["log"] + [line])[-60:]


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if request.url.path == "/healthz" or not APP_PASSWORD:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    ok = False
    if header.lower().startswith("basic "):
        try:
            user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
            ok = secrets.compare_digest(user, APP_USER) and secrets.compare_digest(pwd, APP_PASSWORD)
        except Exception:
            ok = False
    if not ok:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Funcional Lumos"'})
    return await call_next(request)


def run_sync():
    if state["running"]:
        return
    state["running"] = True
    try:
        new_ids = mail.sync(log=log)
        state["last_sync"] = datetime.now().isoformat(timespec="seconds")
        state["last_error"] = None
        log(f"Sincronização concluída. Novos e-mails da Motiva: {len(new_ids)}")
        if AUTO_ANALYZE and ai.API_KEY:
            for eid in new_ids:
                try:
                    ai.analyze(eid)
                    log(f"Briefing gerado para o e-mail {eid}")
                except Exception as ex:
                    log(f"Falha ao analisar o e-mail {eid}: {ex}")
    except Exception as ex:
        state["last_error"] = str(ex)
        log(f"Erro na sincronização: {ex}")
    finally:
        state["running"] = False


def scheduler():
    while True:
        if mail.configured():
            run_sync()
        time.sleep(POLL_MINUTES * 60)


@app.on_event("startup")
def startup():
    init_db()
    threading.Thread(target=scheduler, daemon=True).start()


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# ---------- status ----------
@app.get("/api/status")
def status():
    with SessionLocal() as s:
        counts = {
            "novos": s.query(Email).filter(Email.direction == "in", Email.status == "novo").count(),
            "analisados": s.query(Email).filter(Email.direction == "in", Email.status == "analisado").count(),
            "emails": s.query(Email).count(),
            "decisoes_vigentes": s.query(Decision).filter(Decision.status == "vigente").count(),
            "decisoes_propostas": s.query(Decision).filter(Decision.status == "proposta").count(),
            "documentos": s.query(Document).count(),
        }
        last_db = get_setting(s, "last_sync")
    return {"zoho": mail.configured(), "zoho_user": mail.IMAP_USER, "ia": bool(ai.API_KEY),
            "modelo": f"{ai.PROVIDER}:{ai.MODEL}", "dominios_motiva": mail.MOTIVA_DOMAINS,
            "poll_minutos": POLL_MINUTES, "auto_analise": AUTO_ANALYZE, "ultima_sync": last_db,
            "sincronizando": state["running"], "erro": state["last_error"], "log": state["log"][-25:], **counts}


@app.post("/api/sync")
def sync_now():
    if not mail.configured():
        raise HTTPException(400, "Configure ZOHO_EMAIL e ZOHO_APP_PASSWORD para sincronizar.")
    threading.Thread(target=run_sync, daemon=True).start()
    return {"ok": True}


# ---------- caixa ----------
@app.get("/api/threads")
def threads(filtro: str = "pendentes", q: str = ""):
    with SessionLocal() as s:
        rows = s.query(Email).order_by(Email.date.desc()).limit(3000).all()
    by = {}
    for e in rows:
        t = by.setdefault(e.thread_key, {"thread_key": e.thread_key, "subject": e.subject, "last_date": e.date,
                                          "count": 0, "pending": 0, "analyzed": 0, "last_from": None, "last_in_id": None})
        t["count"] += 1
        if e.direction == "in" and e.status in ("novo", "analisado"):
            t["pending"] += 1
            if e.status == "analisado":
                t["analyzed"] += 1
        if t["last_from"] is None:
            t["last_from"] = "StartGi" if e.direction == "out" else e.from_addr
        if e.direction == "in" and t["last_in_id"] is None:
            t["last_in_id"] = e.id
    out = list(by.values())
    if filtro == "pendentes":
        out = [t for t in out if t["pending"]]
    if q:
        ql = q.lower()
        out = [t for t in out if ql in (t["subject"] or "").lower() or ql in (t["last_from"] or "").lower()]
    out.sort(key=lambda t: t["last_date"] or datetime.min, reverse=True)
    for t in out:
        t["last_date"] = t["last_date"].isoformat() if t["last_date"] else None
    return out[:300]


def email_dict(e, full=True):
    d = {"id": e.id, "direction": e.direction, "from": e.from_addr, "to": e.to_addr, "cc": e.cc_addr,
         "subject": e.subject, "date": e.date.isoformat() if e.date else None, "status": e.status}
    if full:
        d["body"] = e.body_clean or e.body
        d["body_full"] = e.body
    return d


def analysis_dict(a):
    return {"id": a.id, "email_id": a.email_id, "briefing": json.loads(a.briefing_json or "{}"),
            "draft_subject": a.draft_subject, "draft_body": a.draft_body, "final_body": a.final_body,
            "model": a.model, "sources": json.loads(a.sources_json or "[]"),
            "created_at": a.created_at.isoformat() if a.created_at else None}


@app.get("/api/threads/{thread_key:path}")
def thread_detail(thread_key: str):
    with SessionLocal() as s:
        emails = s.query(Email).filter(Email.thread_key == thread_key).order_by(Email.date).all()
        if not emails:
            raise HTTPException(404, "Conversa não encontrada.")
        ids = [e.id for e in emails]
        analyses = s.query(Analysis).filter(Analysis.email_id.in_(ids)).order_by(Analysis.id).all()
        return {"emails": [email_dict(e) for e in emails], "analyses": [analysis_dict(a) for a in analyses]}


@app.post("/api/emails/{email_id}/analyze")
def analyze_email(email_id: int):
    try:
        aid = ai.analyze(email_id)
    except Exception as ex:
        traceback.print_exc()
        raise HTTPException(500, f"Não foi possível gerar o briefing: {ex}")
    with SessionLocal() as s:
        return analysis_dict(s.get(Analysis, aid))


class RewriteIn(BaseModel):
    instrucao: str
    corpo_atual: str | None = None


@app.post("/api/analyses/{analysis_id}/rewrite")
def rewrite(analysis_id: int, body: RewriteIn):
    try:
        return {"draft_body": ai.rewrite(analysis_id, body.instrucao, body.corpo_atual)}
    except Exception as ex:
        raise HTTPException(500, f"Não foi possível reescrever: {ex}")


class RespondIn(BaseModel):
    corpo_final: str


@app.post("/api/analyses/{analysis_id}/sent")
def mark_sent(analysis_id: int, body: RespondIn):
    with SessionLocal() as s:
        a = s.get(Analysis, analysis_id)
        if not a:
            raise HTTPException(404, "Análise não encontrada.")
        a.final_body = body.corpo_final
        e = a.email
        s.query(Email).filter(Email.thread_key == e.thread_key, Email.direction == "in",
                              Email.status.in_(["novo", "analisado"])).update({"status": "respondido"},
                                                                              synchronize_session=False)
        s.commit()
    return {"ok": True}


@app.post("/api/emails/{email_id}/status")
def set_email_status(email_id: int, status: str = Form(...)):
    if status not in ("novo", "analisado", "respondido", "ignorado"):
        raise HTTPException(400, "Status inválido.")
    with SessionLocal() as s:
        e = s.get(Email, email_id)
        e.status = status
        s.commit()
    return {"ok": True}


class ManualEmailIn(BaseModel):
    remetente: str
    assunto: str
    corpo: str
    direcao: str = "in"


@app.post("/api/emails/manual")
def manual_email(body: ManualEmailIn):
    """Cola um e-mail manualmente (útil antes de conectar o Zoho, ou para mensagens de WhatsApp)."""
    with SessionLocal() as s:
        now = datetime.utcnow()
        key = normalize_subject(body.assunto)
        e = Email(message_id=f"manual-{now.timestamp()}", thread_key=key, folder="manual",
                  direction="out" if body.direcao == "out" else "in", from_addr=body.remetente,
                  to_addr="", cc_addr="", subject=body.assunto, date=now, body=body.corpo,
                  body_clean=strip_quoted(body.corpo) or body.corpo, is_motiva=True,
                  status="novo" if body.direcao != "out" else "respondido")
        s.add(e)
        s.flush()
        index_email(s, e)
        s.commit()
        mark_dirty()
        return {"id": e.id, "thread_key": key}


# ---------- decisões ----------
class DecisionIn(BaseModel):
    titulo: str
    modulo: str = ""
    regra: str
    fonte: str = ""
    data_decisao: str = ""
    aprovado_por: str = ""
    status: str = "vigente"
    substituida_por: int | None = None
    notas: str = ""


def decision_dict(d):
    return {c: getattr(d, c) for c in ("id", "titulo", "modulo", "regra", "fonte", "data_decisao", "aprovado_por",
                                       "status", "substituida_por", "notas", "origem_email_id")} | {
        "updated_at": d.updated_at.isoformat() if d.updated_at else None}


@app.get("/api/decisions")
def list_decisions(status: str = ""):
    with SessionLocal() as s:
        q = s.query(Decision)
        if status:
            q = q.filter(Decision.status == status)
        return [decision_dict(d) for d in q.order_by(Decision.modulo, Decision.id.desc()).all()]


@app.post("/api/decisions")
def create_decision(body: DecisionIn):
    with SessionLocal() as s:
        d = Decision(**body.model_dump())
        s.add(d)
        s.commit()
        return decision_dict(d)


@app.put("/api/decisions/{decision_id}")
def update_decision(decision_id: int, body: DecisionIn):
    with SessionLocal() as s:
        d = s.get(Decision, decision_id)
        if not d:
            raise HTTPException(404, "Decisão não encontrada.")
        for k, v in body.model_dump().items():
            setattr(d, k, v)
        if body.substituida_por:
            d.status = "substituida"
        s.commit()
        return decision_dict(d)


# ---------- base de conhecimento ----------
def read_upload(name, raw):
    low = name.lower()
    if low.endswith(".pdf"):
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages)
    if low.endswith(".docx"):
        import docx
        d = docx.Document(io.BytesIO(raw))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for r in t.rows:
                parts.append(" | ".join(c.text for c in r.cells))
        return "\n".join(parts)
    return raw.decode("utf-8", errors="replace")


@app.get("/api/documents")
def list_documents():
    with SessionLocal() as s:
        return [{"id": d.id, "nome": d.nome, "tipo": d.tipo, "tamanho": len(d.content or ""),
                 "created_at": d.created_at.isoformat() if d.created_at else None}
                for d in s.query(Document).order_by(Document.id.desc()).all()]


@app.post("/api/documents")
async def upload_document(tipo: str = Form("documentacao"), nome: str = Form(""), texto: str = Form(""),
                          arquivo: UploadFile | None = File(None)):
    content, filename = texto, nome
    if arquivo is not None and arquivo.filename:
        raw = await arquivo.read()
        try:
            content = read_upload(arquivo.filename, raw)
        except Exception as ex:
            raise HTTPException(400, f"Não consegui ler o arquivo: {ex}")
        filename = nome or arquivo.filename
    if not (content or "").strip():
        raise HTTPException(400, "Envie um arquivo ou cole o texto.")
    with SessionLocal() as s:
        d = Document(nome=filename or "Sem nome", tipo=tipo, content=content)
        s.add(d)
        s.flush()
        ai.index_document(s, d)
        s.commit()
        return {"id": d.id, "nome": d.nome, "tamanho": len(content)}


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int):
    from .db import Chunk
    with SessionLocal() as s:
        s.query(Chunk).filter(Chunk.source_kind == "document", Chunk.source_id == doc_id).delete()
        d = s.get(Document, doc_id)
        if d:
            s.delete(d)
        s.commit()
    mark_dirty()
    return {"ok": True}
