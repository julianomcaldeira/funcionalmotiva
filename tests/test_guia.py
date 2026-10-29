from datetime import datetime

from app import ai
from app.db import Document, Email, SessionLocal
from app.main import seed_guide


def test_guia_criado_editado_e_sempre_no_contexto(client):
    seed_guide()
    seed_guide()  # não duplica
    g = client.get("/api/guia-compras").json()
    assert g["exists"] and g["a_confirmar"] > 0
    assert all(d["tipo"] != "guia_compras" for d in client.get("/api/documents").json())

    r = client.put("/api/guia-compras", json={"content": "Guia novo: RC nasce no SAP."})
    assert r.status_code == 200 and r.json()["a_confirmar"] == 0
    with SessionLocal() as s:
        assert s.query(Document).filter(Document.tipo == "guia_compras").count() == 1
        s.add(Email(id=1, message_id="g1", thread_key="g", direction="in", from_addr="a@motiva.com.br",
                    subject="SLA", date=datetime.utcnow(), body="Qual o SLA?"))
        s.commit()
        ctx, src = ai.build_context(s, s.get(Email, 1))
        assert "RC nasce no SAP" in ctx and any(x["label"] == "Guia de Compras" for x in src)

    r = client.post("/api/documents", data={"tipo": "guia_compras", "nome": "x", "texto": "y"})
    assert r.status_code == 400
