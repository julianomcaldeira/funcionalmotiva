from datetime import datetime, timedelta

from app.db import Analysis, Decision, Email, SessionLocal


def _email(thread, mid, direction, date, subject=None, status="novo"):
    with SessionLocal() as s:
        s.add(Email(message_id=mid, thread_key=thread, folder="INBOX", direction=direction,
                    from_addr="juliana@motiva.com.br" if direction == "in" else "startgi@startgi.com.br",
                    to_addr="startgi@startgi.com.br", subject=subject or f"Assunto {thread}", date=date, body="corpo",
                    body_clean="corpo", is_motiva=(direction == "in"), status=status))
        s.commit()


def _decision(titulo="Regra", modulo="Coupa", fonte="", status="vigente"):
    with SessionLocal() as s:
        d = Decision(titulo=titulo, modulo=modulo, regra="regra", fonte=fonte, status=status)
        s.add(d)
        s.commit()
        return d.id


def _analysis(doc_id):
    with SessionLocal() as s:
        s.add(Analysis(email_id=None, briefing_json="{}",
                       sources_json=f'[{{"code": "DOC{doc_id}", "label": "x"}}]'))
        s.commit()


def test_threads_paginacao(client):
    now = datetime.utcnow()
    for i in range(5):
        _email(f"tk{i}", f"mid{i}", "in", now - timedelta(hours=i), f"Conversa {i}")
    p1 = client.get("/api/threads", params={"filtro": "todas", "per": 2, "page": 1}).json()
    assert p1["total"] == 5
    assert len(p1["items"]) == 2
    p2 = client.get("/api/threads", params={"filtro": "todas", "per": 2, "page": 2}).json()
    p3 = client.get("/api/threads", params={"filtro": "todas", "per": 2, "page": 3}).json()
    p4 = client.get("/api/threads", params={"filtro": "todas", "per": 2, "page": 4}).json()
    keys = [i["thread_key"] for i in p1["items"] + p2["items"] + p3["items"]]
    assert len(keys) == len(set(keys)) == 5
    assert p4["items"] == []
    # limite de página suavizado
    big = client.get("/api/threads", params={"per": 500}).json()
    assert big["per"] == 100


def test_dashboard_analitico(client):
    now = datetime.utcnow()
    _email("ra", "ma1", "in", now - timedelta(hours=4))
    _email("ra", "ma2", "out", now - timedelta(hours=3))
    _email("rb", "mb1", "in", now - timedelta(days=1))
    _email("rb", "mb2", "out", now - timedelta(hours=20))
    _decision(modulo="Coupa")
    _decision(modulo="Coupa")
    _decision(modulo="LOF", status="descartada")

    d = client.get("/api/dashboard").json()
    assert d["tempo_medio_resposta_h"] is not None
    assert len(d["por_mes"]) == 6
    assert all(p["mes"] for p in d["por_mes"])
    mods = d["decisoes_por_modulo"]
    assert mods and mods[0]["modulo"] == "Coupa" and mods[0]["total"] == 2
    assert all(m["modulo"] != "LOF" for m in mods)  # descartada não entra


def test_cobertura(client):
    doc = client.post("/api/documents", data={"tipo": "especificacao", "nome": "EF-X",
                                              "texto": "conteúdo indexável do documento"}).json()
    _analysis(doc["id"])
    sem = _decision(titulo="Sem fonte", modulo="LOF", fonte="")
    com = _decision(titulo="Com fonte", modulo="Coupa", fonte="Email")
    _decision(titulo="Descartada sem fonte", modulo="LOF", fonte="", status="descartada")

    c = client.get("/api/cobertura").json()
    docs = {x["id"]: x for x in c["documentos"]}
    assert doc["id"] in docs
    assert docs[doc["id"]]["citacoes"] == 1
    assert docs[doc["id"]]["chunks"] > 0
    assert docs[doc["id"]]["ultima_citacao"]
    ids_sem_fonte = {x["id"] for x in c["decisoes"]["sem_fonte"]}
    assert sem in ids_sem_fonte and com not in ids_sem_fonte
    ids_nunca = {x["id"] for x in c["decisoes"]["nunca_citadas"]}
    assert com in ids_nunca
    assert c["decisoes"]["ativas"] == 2