"""Leitura do Zoho Mail via IMAP, somente leitura (abre as pastas com readonly=True)."""
import email
import imaplib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape

from .db import Chunk, Email, SessionLocal, get_setting, set_setting
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
    return normalize_subject(subject)


def index_email(session, e):
    session.query(Chunk).filter(Chunk.source_kind == "email", Chunk.source_id == e.id).delete()
    who = "StartGi" if e.direction == "out" else e.from_addr
    label = f"E-mail {e.date:%d/%m/%Y} | {who} | {e.subject}"
    for piece in chunk_text(e.body_clean or e.body):
        session.add(Chunk(source_kind="email", source_id=e.id, label=label, text=piece))


def _connect():
    M = imaplib.IMAP4_SSL(IMAP_HOST, timeout=60)
    M.login(IMAP_USER, IMAP_PASS)
    return M


UID_RE = re.compile(rb"UID (\d+)")


def _store_message(s, raw, folder, initial, log):
    """Grava um e-mail se ele envolver a Motiva. Retorna o Email criado ou None."""
    msg = email.message_from_bytes(raw)
    mid = (msg.get("Message-ID") or "").strip()
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
        dt = datetime.utcnow()
    body = extract_body(msg)
    sent_by_me = IMAP_USER.lower() in [a.lower() for a in frm]
    direction = "out" if (folder.lower() in SENT_FOLDERS or sent_by_me) else "in"
    if direction == "out":
        status = "respondido"
    elif initial and dt < datetime.utcnow() - timedelta(days=HISTORY_DAYS):
        status = "arquivado"  # histórico da carga inicial: entra como contexto, não como pendência
    else:
        status = "novo"
    e = Email(message_id=mid, thread_key=thread_key_for(s, msg, subject), folder=folder,
              direction=direction, from_addr=", ".join(frm), to_addr=", ".join(to),
              cc_addr=", ".join(cc), subject=subject, date=dt, body=body,
              body_clean=strip_quoted(body), is_motiva=True, status=status)
    s.add(e)
    s.flush()
    index_email(s, e)
    if direction == "out":  # resposta enviada: fecha as pendências anteriores da thread
        s.query(Email).filter(Email.thread_key == e.thread_key, Email.direction == "in",
                              Email.date <= dt, Email.status.in_(["novo", "analisado"])) \
            .update({"status": "respondido"}, synchronize_session=False)
    s.commit()
    return e


def _sync_folder(s, folder, since_str, initial, log, new_inbound):
    """Lê uma pasta em lotes, por UID, guardando o progresso. Retorna False se a pasta não existe."""
    M = _connect()
    try:
        typ, _ = M.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            log(f"Pasta não encontrada: {folder}")
            return False
        uidv = (M.untagged_responses.get("UIDVALIDITY") or [b"0"])[0]
        uidv = uidv.decode() if isinstance(uidv, bytes) else str(uidv)
        if get_setting(s, f"uidv:{folder}") != uidv:  # pasta recriada no servidor: recomeça a contagem
            set_setting(s, f"uidv:{folder}", uidv)
            set_setting(s, f"uid:{folder}", "0")
        last_uid = int(get_setting(s, f"uid:{folder}", "0") or 0)
        if last_uid:
            typ, data = M.uid("search", None, "UID", f"{last_uid + 1}:*")
        else:
            typ, data = M.uid("search", None, "SINCE", since_str)
        uids = sorted(int(u) for u in (data[0].split() if typ == "OK" and data and data[0] else []) if int(u) > last_uid)
        if not uids:
            return True
        log(f"{folder}: {len(uids)} mensagens para verificar")
        saved = 0
        for i in range(0, len(uids), BATCH):
            chunk = uids[i:i + BATCH]
            typ, data = M.uid("fetch", ",".join(map(str, chunk)),
                              "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM TO CC)])")
            wanted = []
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
                    wanted.append(m.group(1).decode())
            for uid in wanted:
                typ, full = M.uid("fetch", uid, "(BODY.PEEK[])")
                raw = next((it[1] for it in (full or []) if isinstance(it, tuple)), None)
                if raw:
                    e = _store_message(s, raw, folder, initial, log)
                    if e:
                        saved += 1
                        if e.direction == "in" and e.status == "novo" and not initial:
                            new_inbound.append(e.id)
            set_setting(s, f"uid:{folder}", str(chunk[-1]))  # progresso salvo a cada lote
        log(f"{folder}: {saved} e-mails da Motiva importados")
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
        for folder in FOLDERS:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    _sync_folder(s, folder, since_str, initial, log, new_inbound)
                    break
                except (imaplib.IMAP4.abort, OSError, EOFError) as ex:
                    s.rollback()
                    log(f"{folder}: conexão interrompida pelo Zoho ({ex}). Retomando de onde parou ({attempt}/{MAX_RETRIES})")
                    time.sleep(3 * attempt)
            else:
                raise RuntimeError(f"O Zoho interrompeu a leitura da pasta {folder} várias vezes. "
                                   "O progresso foi salvo; a próxima leitura continua de onde parou.")
        set_setting(s, "last_sync", datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
    mark_dirty()
    return new_inbound
