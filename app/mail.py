"""Leitura do Zoho Mail via IMAP, somente leitura (abre as pastas com readonly=True)."""
import email
import imaplib
import os
import re
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


def sync(log=print):
    """Busca e-mails novos. Retorna a lista de ids de e-mails recebidos da Motiva que são novos."""
    if not configured():
        raise RuntimeError("Zoho não configurado: defina ZOHO_EMAIL e ZOHO_APP_PASSWORD.")
    new_inbound = []
    with SessionLocal() as s:
        last = get_setting(s, "last_sync")
        since = (datetime.fromisoformat(last) - timedelta(days=2)) if last else datetime.now(timezone.utc) - timedelta(days=FIRST_SYNC_DAYS)
        since_str = since.strftime("%d-%b-%Y")
        M = imaplib.IMAP4_SSL(IMAP_HOST)
        M.login(IMAP_USER, IMAP_PASS)
        try:
            for folder in FOLDERS:
                typ, _ = M.select(f'"{folder}"', readonly=True)
                if typ != "OK":
                    log(f"Pasta não encontrada: {folder}")
                    continue
                typ, data = M.search(None, "SINCE", since_str)
                ids = data[0].split() if typ == "OK" else []
                log(f"{folder}: {len(ids)} mensagens desde {since_str}")
                for num in ids:
                    typ, hdr = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                    mid_raw = hdr[0][1].decode(errors="ignore") if hdr and hdr[0] else ""
                    mid = mid_raw.split(":", 1)[-1].strip() if ":" in mid_raw else ""
                    if mid and s.query(Email.id).filter(Email.message_id == mid).first():
                        continue
                    typ, full = M.fetch(num, "(BODY.PEEK[])")
                    if typ != "OK" or not full or not full[0]:
                        continue
                    msg = email.message_from_bytes(full[0][1])
                    mid = (msg.get("Message-ID") or mid or f"{folder}-{num.decode()}").strip()
                    if s.query(Email.id).filter(Email.message_id == mid).first():
                        continue
                    subject = dec(msg.get("Subject"))
                    if SUBJECT_FILTER and SUBJECT_FILTER not in subject.lower():
                        continue
                    frm = [a for _, a in getaddresses([dec(msg.get("From"))])]
                    to = [a for _, a in getaddresses([dec(msg.get("To"))])]
                    cc = [a for _, a in getaddresses([dec(msg.get("Cc"))])]
                    if not is_motiva(frm + to + cc):
                        continue  # só guarda o que envolve a Motiva
                    try:
                        dt = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).replace(tzinfo=None)
                    except Exception:
                        dt = datetime.utcnow()
                    body = extract_body(msg)
                    direction = "out" if folder.lower() in SENT_FOLDERS else "in"
                    e = Email(message_id=mid, thread_key=thread_key_for(s, msg, subject), folder=folder,
                              direction=direction, from_addr=", ".join(frm), to_addr=", ".join(to),
                              cc_addr=", ".join(cc), subject=subject, date=dt, body=body,
                              body_clean=strip_quoted(body), is_motiva=True,
                              status="novo" if direction == "in" else "respondido")
                    s.add(e)
                    s.flush()
                    index_email(s, e)
                    if direction == "out":  # resposta enviada: fecha as pendências anteriores da thread
                        s.query(Email).filter(Email.thread_key == e.thread_key, Email.direction == "in",
                                              Email.date <= dt, Email.status.in_(["novo", "analisado"])) \
                            .update({"status": "respondido"}, synchronize_session=False)
                    s.commit()
                    if direction == "in" and last:  # na primeira carga não dispara análise do histórico
                        new_inbound.append(e.id)
            set_setting(s, "last_sync", datetime.now(timezone.utc).replace(tzinfo=None).isoformat())
        finally:
            try:
                M.logout()
            except Exception:
                pass
    mark_dirty()
    return new_inbound
