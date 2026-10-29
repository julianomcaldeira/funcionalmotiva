"""Leitura do Zoho com um servidor IMAP simulado: todas as pastas, e-mail com erro não trava, conferência."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from app import mail
from app.db import Email, SessionLocal, set_setting


def raw(mid, subj, frm="karina@motiva.com.br", to="juliano@startgi.com.br", hours=2):
    d = format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours))
    return (f"Message-ID: <{mid}>\r\nFrom: {frm}\r\nTo: {to}\r\nSubject: {subj}\r\nDate: {d}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\nCorpo de {subj}\r\n").encode()


class FakeIMAP:
    def __init__(self, boxes):
        self.boxes, self.cur = boxes, None
        self.untagged_responses = {}

    def list(self):
        return "OK", [f'(\\HasNoChildren) "/" "{n}"'.encode() for n in self.boxes]

    def select(self, name, readonly=True):
        n = name.strip('"')
        if n not in self.boxes:
            return "NO", [b""]
        self.cur = n
        self.untagged_responses = {"UIDVALIDITY": [b"7"]}
        return "OK", [str(len(self.boxes[n])).encode()]

    def uid(self, cmd, *args):
        msgs = self.boxes[self.cur]
        if cmd == "search":
            if args[1] == "UID":
                lo = int(args[2].split(":")[0])
                ids = [u for u in msgs if u >= lo] or ([max(msgs)] if msgs else [])
            else:
                ids = list(msgs)
            return "OK", [" ".join(map(str, ids)).encode()]
        if cmd == "fetch":
            out = []
            for u in map(int, args[0].split(",")):
                if u in msgs:
                    body = msgs[u]
                    if "HEADER" in args[1]:
                        body = body.split(b"\r\n\r\n")[0] + b"\r\n\r\n"
                    out.append((f"{u} (UID {u} BODY[] {{{len(body)}}}".encode(), body))
                    out.append(b")")
            return "OK", out
        raise AssertionError(cmd)

    def logout(self):
        pass


@pytest.fixture
def zoho(monkeypatch):
    boxes = {
        "INBOX": {1: raw("a1", "Painel"), 2: raw("x1", "Newsletter", frm="news@outro.com")},
        "Sent": {5: raw("s1", "Re: Painel", frm="juliano@startgi.com.br", to="karina@motiva.com.br", hours=1)},
        "MOTIVA - CCR/Supply": {9: raw("b1", "SLA Supply"), 10: raw("bad", "Quebrado")},
        "Trash": {3: raw("t1", "Apagado")},
        "Pessoal/Banco": {4: raw("p1", "Extrato", frm="banco@banco.com"), 6: raw("p2", "Motiva no pessoal")},
    }
    fake = FakeIMAP(boxes)
    monkeypatch.setattr(mail, "_connect", lambda: fake)
    monkeypatch.setattr(mail, "IMAP_USER", "juliano@startgi.com.br")
    monkeypatch.setattr(mail, "IMAP_PASS", "x")
    monkeypatch.setattr(mail, "ALL_FOLDERS", True)
    monkeypatch.setattr(mail, "FOLDERS", ["INBOX", "Sent"])
    orig = mail._store_message

    def store(s, r, folder, initial, log):
        if b"<bad>" in r and not getattr(store, "fixed", False):
            raise ValueError("anexo corrompido")
        return orig(s, r, folder, initial, log)
    monkeypatch.setattr(mail, "_store_message", store)
    with SessionLocal() as s:
        set_setting(s, "last_sync", "2026-01-01T00:00:00")  # não é a carga inicial
    return boxes, store


def mids():
    with SessionLocal() as s:
        return {m for (m,) in s.query(Email.message_id).all()}


def test_le_subpastas_ignora_lixeira_e_nao_trava(zoho):
    boxes, store = zoho
    logs = []
    mail.sync(log=logs.append)
    got = mids()
    assert {"<a1>", "<s1>", "<b1>"} <= got            # subpasta lida, Sent lido
    assert "<t1>" not in got and "<x1>" not in got    # lixeira e e-mail de fora ignorados
    assert "<bad>" not in got and any("UID 10" in l for l in logs)  # erro registrado, sem travar

    store.fixed = True                                # na leitura seguinte o e-mail com erro entra
    mail.sync(log=logs.append)
    assert "<bad>" in mids()


def test_rede_de_seguranca_recupera_email_perdido(zoho):
    boxes, store = zoho
    store.fixed = True
    mail.sync(log=lambda *_: None)
    with SessionLocal() as s:                         # simula um e-mail que "escapou"
        s.query(Email).filter(Email.message_id == "<a1>").delete()
        s.commit()
    assert mail.check_missing(1)["total_faltando"] == 1
    mail.sync(log=lambda *_: None)
    assert "<a1>" in mids()
    assert mail.check_missing(1)["total_faltando"] == 0


def test_conferencia_e_log_nao_expoem_pastas_pessoais(zoho):
    boxes, store = zoho
    store.fixed = True
    logs = []
    mail.sync(log=logs.append)
    r = mail.check_missing(1)
    nomes = {p["pasta"] for p in r["pastas"]}
    assert "Pessoal/Banco" not in nomes and "outra pasta" in nomes   # e-mail da Motiva em pasta pessoal: nome oculto
    assert all("mensagens" not in p for p in r["pastas"])            # sem contagem de e-mails que não são da Motiva
    assert not any("Pessoal" in l or "Banco" in l for l in logs)
    assert "<p1>" not in mids()                                       # e-mail pessoal nunca é gravado
