import json
from datetime import datetime, timedelta

from app import ai
from app.db import AIUsage, Analysis, Email, SessionLocal


def _email(i, thread, hours, status="analisado", direction="in"):
    return Email(id=i, message_id=f"q{i}", thread_key=thread, direction=direction, from_addr="a@motiva.com.br",
                 subject=f"Assunto {i}", date=datetime.utcnow() - timedelta(hours=hours), body="x", status=status)


def test_fila_prazo_e_urgencia(client):
    with SessionLocal() as s:
        s.add_all([_email(1, "urg", 30), _email(2, "norm", 30), _email(3, "velho", 80, status="novo"),
                   _email(4, "ok", 5, status="respondido")])
        s.add(Analysis(email_id=1, briefing_json=json.dumps({"pedidos": [{"texto": "x", "urgencia": "alta"}],
                                                             "perguntas": ["a", "b"]})))
        s.add(Analysis(email_id=2, briefing_json=json.dumps({"pedidos": [{"texto": "x", "urgencia": "media"}]})))
        s.commit()
    d = client.get("/api/dashboard").json()
    fila = {t["thread_key"]: t for t in d["fila"]}
    assert "ok" not in fila
    assert fila["urg"]["urgencia"] == "alta" and fila["urg"]["atrasado"] and fila["urg"]["perguntas"] == 2
    assert not fila["norm"]["atrasado"]                     # 30h < 72h
    assert fila["velho"]["atrasado"] and not fila["velho"]["briefing"]
    assert d["fila"][0]["atrasado"]                         # atrasadas primeiro


def test_saude_ia(monkeypatch):
    monkeypatch.setattr(ai, "API_KEY", "x")
    assert ai.health()["estado"] == "sem_uso"
    with SessionLocal() as s:
        s.add(AIUsage(tipo="analise", ok=True))
        s.add(AIUsage(tipo="assistente", ok=False, erro="IA respondeu HTTP 401: Invalid Authorization"))
        s.commit()
    h = ai.health()
    assert h["estado"] == "erro" and "chave" in h["mensagem"]
    with SessionLocal() as s:
        s.add(AIUsage(tipo="analise", ok=True))
        s.commit()
    assert ai.health()["estado"] == "ok"


def test_falha_da_ia_fica_registrada(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("IA respondeu HTTP 401")
    monkeypatch.setattr(ai, "_call_model", boom)
    try:
        ai.call_model("s", "u", kind="analise")
    except RuntimeError:
        pass
    with SessionLocal() as s:
        u = s.query(AIUsage).order_by(AIUsage.id.desc()).first()
        assert not u.ok and "401" in u.erro


def test_painel_dados(client):
    with SessionLocal() as s:
        s.add_all([_email(11, "p1", 30, status="respondido"), _email(12, "p1", 20, status="respondido", direction="out"),
                   _email(13, "p2", 5, status="novo")])
        s.commit()
    p = client.get("/api/painel", params={"dias": 7}).json()
    assert p["recebidos"]["atual"] == 2 and p["enviados"]["atual"] == 1
    assert p["tempo_resposta_h"]["atual"] == 10.0
    assert p["pendentes"] == 1 and len(p["serie"]) == 7
    assert p["remetentes"][0]["total"] == 2
