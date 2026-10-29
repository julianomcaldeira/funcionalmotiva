"""Leitura do Zoho Mail via IMAP, somente leitura (abre as pastas com readonly=True)."""
import email
import hashlib
import imaplib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape

from .db import Attachment, Chunk, Email, SessionLocal, get_setting, set_setting
from .extract import extract_text_from_bytes
from .retrieval import chunk_text, mark_dirty

IMAP_HOST = os.getenv("ZOHO_IMAP_HOST", "imap.zoho.com")
IMAP_USER = os.getenv("ZOHO_EMAIL", "")
IMAP_PASS = os.getenv("ZOHO_APP_PASSWORD", "")
FOLDERS = [f.strip() for f in os.getenv("ZOHO_FOLDERS", "INBOX,Sent").split(",") if f.strip()]
SENT_FOLDERS = {f.strip().lower() for f in os.getenv("ZOHO_SENT_FOLDERS", "Sent").split(",")}
MOTIVA_DOMAINS = [d.strip().lower() for d in os.getenv("MOTIVA_DOMAINS", "motiva.com.br,grupoccr.com.br").split(",") if d.strip()]
FIRST_SYNC_DAYS = int(os.getenv("ZOHO_FIRST_SYNC_DAYS", "365"))
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "30"))  # na carga inicial, recebidos mais antigos que isso viram histórico
BATCH = int(os.getenv("ZOHO_BATCH", "100"))
MAX_RETRIES = int(os.getenv("ZOHO_MAX_RETRIES", "8"))
SUBJECT_FILTER = os.getenv("SUBJECT_FILTER", "").strip().lower()  # opcional: só assuntos que contenham este termo
# Lê TODAS as pastas da conta (menos lixeira, spam e rascunhos), para não perder e-mail movido por regra/filtro.
# ZOHO_ALL_FOLDERS=false volta a ler só as pastas de ZOHO_FOLDERS.
ALL_FOLDERS = os.getenv("ZOHO_ALL_FOLDERS", "true").lower() == "true"
SKIP_FOLDERS = {f.strip().lower() for f in os.getenv(
    "ZOHO_SKIP_FOLDERS", "Trash,Spam,Junk,Drafts,Templates,Outbox,Lixeira,Rascunhos,Notification").split(",") if f.strip()}
RECHECK_DAYS = int(os.getenv("ZOHO_RECHECK_DAYS", "3"))  # rede de segurança: reconfere os últimos N dias
LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')

PREFIX_RE = re.compile(r"^\s*((re|res|enc|fw|fwd|rv|tr)\s*:\s*)+", re.I)


def configured():
    return bool(IMAP_USER and IMAP_PASS)


def dec(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def normalize_subject(subject):
    s = PREFIX_RE.sub("", subject or "").strip()
    return re.sub(r"\s+", " ", s).lower()[:400] or "(sem assunto)"


def html_to_text(html):
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", "", html))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_body(msg):
    plain, html = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html is None:
            html = text
    if plain and plain.strip():
        return plain.strip()
    return html_to_text(html) if html else ""


def extract_attachments(msg):
    """Anexos de verdade: partes com nome de arquivo, fora de imagens inline da assinatura."""
    atts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        fn = part.get_filename()
        if not fn:
            continue
        if part.get("Content-Id") and part.get_content_maintype() == "image":
            continue  # imagem da assinatura/corpo (referenciada por <img cid:...>), não é anexo
        raw = part.get_payload(decode=True)
        if raw is None:
            continue
        atts.append((os.path.basename(fn) or fn, part.get_content_type() or "", raw))
    seen, out = set(), []
    for fn, ctype, raw in atts:
        key = (fn.lower(), raw[:256])
        if key in seen:
            continue
        seen.add(key)
        out.append((fn, ctype, raw))
    return out


QUOTE_MARKERS = [
    re.compile(r"^\s*Em .{5,200}escreveu:\s*$", re.I | re.M),
    re.compile(r"^\s*On .{5,200}wrote:\s*$", re.I | re.M),
    re.compile(r"^\s*-{2,}\s*(Mensagem original|Original Message|Forwarded message|Mensagem encaminhada)", re.I | re.M),
    re.compile(r"^\s*(De|From)\s*:\s*.+\n\s*(Enviad[ao]|Sent|Data|Date)\s*:", re.I | re.M),
    re.compile(r"^\s*_{10,}\s*$", re.M),
]


def strip_quoted(body):
    cut = len(body)
    for rx in QUOTE_MARKERS:
        m = rx.search(body)
        if m and m.start() > 20:
            cut = min(cut, m.start())
    text = body[:cut]
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
    return text.strip()


def is_motiva(addrs):
    return any(a.lower().split("@")[-1].endswith(tuple(MOTIVA_DOMAINS)) for a in addrs if "@" in a)


def thread_key_for(session, msg, subject):
    refs = (msg.get("References") or "").split() + (msg.get("In-Reply-To") or "").split()
    if refs:
        known = session.query(Email.thread_key).filter(Email.message_id.in_(refs)).first()
        if known:
            return known[0]
        # nada da cadeia está cadastrado: ancora na raiz para não fundir
        # conversas diferentes que tenham o mesmo assunto
        return f"{normalize_subject(subject)}|{refs[0]}"
    return normalize_subject(subject)


def index_email(session, e):
    session.query(Chunk).filter(Chunk.source_kind == "email", Chunk.source_id == e.id).delete()
    who = "StartGi" if e.direction == "out" else e.from_addr
    d = e.date.strftime("%d/%m/%Y") if e.date else "data desconhecida"
    label = f"E-mail {d} | {who} | {e.subject}"
    for piece in chunk_text(e.body_clean or e.body):
        session.add(Chunk(source_kind="email", source_id=e.id, label=label, text=piece))
    for att in e.attachments:
        if att.extracted_text:
            for piece in chunk_text(att.extracted_text):
                session.add(Chunk(source_kind="email", source_id=e.id,
                                  label=f"{label} (anexo: {att.filename})", text=piece))


def _connect():
    M = imaplib.IMAP4_SSL(IMAP_HOST, timeout=60)
    M.login(IMAP_USER, IMAP_PASS)
    return M


UID_RE = re.compile(rb"UID (\d+)")


def _store_message(s, raw, folder, initial, log):
    """Grava um e-mail se ele envolver a Motiva. Retorna o Email criado ou None."""
    msg = email.message_from_bytes(raw)
    mid = (msg.get("Message-ID") or "").strip()
    if not mid:  # mensagens sem Message-ID não podem simplesmente sumir
        mid = f"syn-{hashlib.sha1(raw).hexdigest()}"
    if not mid or s.query(Email.id).filter(Email.message_id == mid).first():
        return None
    subject = dec(msg.get("Subject"))
    if SUBJECT_FILTER and SUBJECT_FILTER not in subject.lower():
        return None
    frm = [a for _, a in getaddresses([dec(msg.get("From"))])]
    to = [a for _, a in getaddresses([dec(msg.get("To"))])]
    cc = [a for _, a in getaddresses([dec(msg.get("Cc"))])]
    if not is_motiva(frm + to + cc):
        return None
    try:
        dt = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        dt = None  # data desconhecida: não inventa "hoje" nem cria pendência nova por causa disso
    body = extract_body(msg)
    sent_by_me = IMAP_USER.lower() in [a.lower() for a in frm]
    direction = "out" if (folder.lower() in SENT_FOLDERS or sent_by_me) else "in"
    if direction == "out":
        status = "respondido"
    elif initial and dt and dt < datetime.utcnow() - timedelta(days=HISTORY_DAYS):
        status = "arquivado"  # histórico da carga inicial: entra como contexto, não como pendência
    else:
        status = "novo"
    e = Email(message_id=mid, thread_key=thread_key_for(s, msg, subject), folder=folder,
              direction=direction, from_addr=", ".join(frm), to_addr=", ".join(to),
              cc_addr=", ".join(cc), subject=subject, date=dt, body=body,
              body_clean=strip_quoted(body), is_motiva=True, status=status)
    s.add(e)
    s.flush()
    for fn, ctype, raw in extract_attachments(msg):
        text = extract_text_from_bytes(fn, raw)
        s.add(Attachment(email_id=e.id, filename=fn[:500], content_type=ctype, size=len(raw),
                         file_data=raw, extracted_text=text or None))
    s.flush()
    index_email(s, e)
    if direction == "out" and dt is not None:  # resposta enviada: fecha as pendências anteriores da thread
        s.query(Email).filter(Email.thread_key == e.thread_key, Email.direction == "in",
                              Email.date <= dt, Email.status.in_(["novo", "analisado"])) \
            .update({"status": "respondido"}, synchronize_session=False)
    s.commit()
    return e


def list_folders(M=None):
    """Pastas a ler: todas as selecionáveis da conta (menos as de SKIP_FOLDERS) ou só ZOHO_FOLDERS."""
    if not ALL_FOLDERS:
        return list(FOLDERS)
    own = M is None
    M = M or _connect()
    try:
        typ, data = M.list()
        names = []
        for raw in data or []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            m = LIST_RE.match(line)
            if not m or "\\noselect" in m.group("flags").lower():
                continue
            name = m.group("name").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"')
            leaf = re.split(r"[/.]", name)[-1].strip().lower()
            if name.lower() in SKIP_FOLDERS or leaf in SKIP_FOLDERS:
                continue
            names.append(name)
        # garante as pastas configuradas mesmo se o LIST vier incompleto
        for f in FOLDERS:
            if f not in names:
                names.append(f)
        return names
    finally:
        if own:
            try:
                M.logout()
            except Exception:
                pass


def _fetch_and_store(M, s, uid, folder, initial, log, new_inbound):
    typ, full = M.uid("fetch", str(uid), "(BODY.PEEK[])")
    raw = next((it[1] for it in (full or []) if isinstance(it, tuple)), None)
    if not raw:
        return None
    e = _store_message(s, raw, folder, initial, log)
    if e and e.direction == "in" and e.status == "novo" and not initial:
        new_inbound.append(e.id)
    return e


def _motiva_uids(M, s, uids):
    """Dos UIDs informados, os que envolvem a Motiva e ainda não estão no sistema (só lê cabeçalhos)."""
    wanted = []
    for i in range(0, len(uids), BATCH):
        chunk = uids[i:i + BATCH]
        typ, data = M.uid("fetch", ",".join(map(str, chunk)),
                          "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM TO CC DATE SUBJECT)])")
        for item in data or []:
            if not isinstance(item, tuple):
                continue
            m = UID_RE.search(item[0])
            if not m:
                continue
            hdr = email.message_from_bytes(item[1])
            addrs = [a for _, a in getaddresses([dec(hdr.get("From")), dec(hdr.get("To")), dec(hdr.get("Cc"))])]
            mid = (hdr.get("Message-ID") or "").strip()
            if is_motiva(addrs) and not (mid and s.query(Email.id).filter(Email.message_id == mid).first()):
                wanted.append((int(m.group(1)), hdr))
    return wanted


def _sync_folder(s, folder, since_str, initial, log, new_inbound):
    """Lê uma pasta por UID guardando o progresso, e reconfere os últimos dias como rede de segurança.
    Um e-mail com problema é registrado e pulado (tenta de novo na próxima leitura) sem travar os demais."""
    M = _connect()
    try:
        typ, _ = M.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            log(f"Pasta não encontrada: {folder}")
            return False
        uidv = (M.untagged_responses.get("UIDVALIDITY") or [b"0"])[0]
        uidv = uidv.decode() if isinstance(uidv, bytes) else str(uidv)
        if get_setting(s, f"uidv:{folder}") != uidv:  # pasta nova ou recriada no servidor: recomeça a contagem
            set_setting(s, f"uidv:{folder}", uidv)
            set_setting(s, f"uid:{folder}", "0")
        last_uid = int(get_setting(s, f"uid:{folder}", "0") or 0)
        # pasta lida pela primeira vez: o que é antigo entra como histórico, não como pendência
        folder_initial = initial or not last_uid
        if last_uid:
            typ, data = M.uid("search", None, "UID", f"{last_uid + 1}:*")
        else:
            typ, data = M.uid("search", None, "SINCE", since_str)
        uids = sorted(int(u) for u in (data[0].split() if typ == "OK" and data and data[0] else []) if int(u) > last_uid)
        try:
            falhas = set(json.loads(get_setting(s, f"falhas:{folder}", "[]") or "[]"))
        except ValueError:
            falhas = set()
        saved = 0
        if uids:
            log(f"{folder}: {len(uids)} mensagens novas para verificar")
        for i in range(0, len(uids), BATCH):
            chunk = uids[i:i + BATCH]
            for uid, _hdr in _motiva_uids(M, s, chunk):
                try:
                    if _fetch_and_store(M, s, uid, folder, folder_initial, log, new_inbound):
                        saved += 1
                    falhas.discard(uid)
                except (imaplib.IMAP4.abort, OSError, EOFError):
                    raise
                except Exception as ex:
                    s.rollback()
                    falhas.add(uid)
                    log(f"{folder}: e-mail UID {uid} não pôde ser importado ({ex}). Vai tentar de novo.")
            set_setting(s, f"uid:{folder}", str(chunk[-1]))  # progresso salvo a cada lote
            set_setting(s, f"falhas:{folder}", json.dumps(sorted(falhas)))
        # rede de segurança: reconfere os últimos dias e as falhas anteriores
        desde = (datetime.now(timezone.utc) - timedelta(days=RECHECK_DAYS)).strftime("%d-%b-%Y")
        typ, data = M.uid("search", None, "SINCE", desde)
        recentes = sorted(int(u) for u in (data[0].split() if typ == "OK" and data and data[0] else []))
        alvo = sorted(set(recentes) | falhas)
        recuperados = 0
        for uid, _hdr in _motiva_uids(M, s, alvo) if alvo else []:
            try:
                if _fetch_and_store(M, s, uid, folder, False, log, new_inbound):
                    recuperados += 1
                falhas.discard(uid)
            except (imaplib.IMAP4.abort, OSError, EOFError):
                raise
            except Exception as ex:
                s.rollback()
                falhas.add(uid)
                log(f"{folder}: e-mail UID {uid} segue com erro ({ex}).")
        set_setting(s, f"falhas:{folder}", json.dumps(sorted(falhas)))
        if saved or recuperados:
            log(f"{folder}: {saved} e-mails da Motiva importados"
                + (f", {recuperados} recuperados na conferência" if recuperados else ""))
        if falhas:
            log(f"{folder}: {len(falhas)} e-mail(s) com erro aguardando nova tentativa")
        return True
    finally:
        try:
            M.logout()
        except Exception:
            pass


def sync(log=print):
    """Busca e-mails novos. Retorna os ids de e-mails recebidos da Motiva que são novos.
    Lê em lotes e guarda o progresso por pasta: se o Zoho derrubar a conexão, reconecta e
    continua de onde parou, sem recomeçar do zero."""
    if not configured():
        raise RuntimeError("Zoho não configurado: defina ZOHO_EMAIL e ZOHO_APP_PASSWORD.")
    new_inbound = []
    with SessionLocal() as s:
        last = get_setting(s, "last_sync")
        initial = not last
        since = datetime.now(timezone.utc) - timedelta(days=FIRST_SYNC_DAYS)
        since_str = since.strftime("%d-%b-%Y")
        if initial:  # e-mails antigos que entraram como pendentes em versões anteriores viram histórico
            cutoff = datetime.utcnow() - timedelta(days=HISTORY_DAYS)
            s.query(Email).filter(Email.direction == "in", Email.status == "novo", Email.date < cutoff) \
                .update({"status": "arquivado"}, synchronize_session=False)
            s.commit()
        folders = list_folders()
        set_setting(s, "pastas_lidas", json.dumps(folders, ensure_ascii=False))
        erros = []
        for folder in folders:  # uma pasta com problema não impede a leitura das outras
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    _sync_folder(s, folder, since_str, initial, log, new_inbound)
                    break
                except (imaplib.IMAP4.abort, OSError, EOFError) as ex:
                    s.rollback()
                    log(f"{folder}: conexão interrompida pelo Zoho ({ex}). Retomando de onde parou ({attempt}/{MAX_RETRIES})")
                    time.sleep(3 * attempt)
                except Exception as ex:
                    s.rollback()
                    log(f"{folder}: erro na leitura ({ex})")
                    erros.append(f"{folder}: {ex}")
                    break
            else:
                erros.append(f"O Zoho interrompeu a leitura da pasta {folder} várias vezes.")
        set_setting(s, "last_sync", datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
    mark_dirty()
    if erros:
        raise RuntimeError("Leitura parcial. " + " | ".join(erros)[:600])
    return new_inbound


def check_missing(days=1):
    """Conferência: compara o que está no Zoho (todas as pastas lidas) com o que está no sistema."""
    if not configured():
        raise RuntimeError("Zoho não configurado.")
    desde = (datetime.now(timezone.utc) - timedelta(days=max(0, days - 1))).strftime("%d-%b-%Y")
    pastas, faltando = [], []
    M = _connect()
    try:
        folders = list_folders(M)
        with SessionLocal() as s:
            for folder in folders:
                typ, _ = M.select(f'"{folder}"', readonly=True)
                if typ != "OK":
                    pastas.append({"pasta": folder, "erro": "não abriu"})
                    continue
                typ, data = M.uid("search", None, "SINCE", desde)
                uids = sorted(int(u) for u in (data[0].split() if typ == "OK" and data and data[0] else []))
                total_motiva = 0
                falt = 0
                for i in range(0, len(uids), BATCH):
                    chunk = uids[i:i + BATCH]
                    typ, hd = M.uid("fetch", ",".join(map(str, chunk)),
                                    "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM TO CC DATE SUBJECT)])")
                    for item in hd or []:
                        if not isinstance(item, tuple):
                            continue
                        hdr = email.message_from_bytes(item[1])
                        addrs = [a for _, a in getaddresses([dec(hdr.get("From")), dec(hdr.get("To")), dec(hdr.get("Cc"))])]
                        if not is_motiva(addrs):
                            continue
                        total_motiva += 1
                        mid = (hdr.get("Message-ID") or "").strip()
                        if mid and s.query(Email.id).filter(Email.message_id == mid).first():
                            continue
                        falt += 1
                        faltando.append({"pasta": folder, "assunto": dec(hdr.get("Subject")),
                                         "de": dec(hdr.get("From")), "data": hdr.get("Date") or ""})
                pastas.append({"pasta": folder, "mensagens": len(uids), "motiva": total_motiva, "faltando": falt})
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return {"desde": desde, "pastas": pastas, "faltando": faltando[:200], "total_faltando": len(faltando)}
