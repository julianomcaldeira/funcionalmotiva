import json

from app import ai
from app.db import AIUsage, Decision, SessionLocal

from .conftest import make_decision


class _D:
    def __init__(self, titulo, regra):
        self.titulo, self.regra = titulo, regra


def test_find_similar_decision():
    ds = [_D("Ordem de prioridade da regra de SLA do Comprador",
             "Prioridade 1 contrato K aprovado, depois contrato F ou LPU U")]
    assert ai.find_similar_decision(ds, "Prioridade da regra de SLA do comprador",
                                    "Prioridade 1 contrato K aprovado; depois contrato F ou LPU U")
    assert not ai.find_similar_decision(ds, "Grupos POOL", "Lista fixa de grupos POOL configurável")


def test_review_and_apply(client, monkeypatch):
    base = client.post("/api/decisions", json=make_decision()).json()["id"]
    pend = client.post("/api/decisions", json=make_decision({"status": "proposta", "titulo": "Outra"})).json()["id"]
    monkeypatch.setattr(ai, "call_model", lambda system, user, **k: json.dumps(
        {"acao": "duplicada", "duplicada_de": base, "confianca": "alta", "justificativa": f"Igual à [D{base}]."}))
    sug = ai.review_decision(pend)
    assert sug["acao"] == "duplicada" and sug["duplicada_de"] == base

    lst = client.get("/api/decisions", params={"status": "proposta"}).json()
    assert lst[0]["sugestao"]["acao"] == "duplicada"
    t = client.get("/api/decisions/triagem").json()
    assert t["pendentes"] == 1 and t["com_sugestao"] == 1

    r = client.post("/api/decisions/aplicar-sugestoes", json={"ids": [pend]})
    assert r.json()["aplicadas"] == 1
    with SessionLocal() as s:
        d = s.get(Decision, pend)
        assert d.status == "substituida" and d.substituida_por == base and "Triagem IA" in d.notas
    assert len(client.get(f"/api/decisions/{pend}/revisions").json()) == 1


def test_invalid_suggestion_falls_back(client, monkeypatch):
    pend = client.post("/api/decisions", json=make_decision({"status": "proposta"})).json()["id"]
    monkeypatch.setattr(ai, "call_model", lambda system, user, **k: '{"acao": "duplicada", "duplicada_de": 99999}')
    assert ai.review_decision(pend)["acao"] == "em_discussao"


def test_usage_endpoint(client):
    with SessionLocal() as s:
        s.add(AIUsage(tipo="analise", modelo="m", tokens_entrada=1_000_000, tokens_saida=1_000_000))
        s.commit()
    u = client.get("/api/uso-ia", params={"dias": 7}).json()
    assert u["hoje"]["chamadas"] >= 1 and u["hoje"]["tokens"] >= 2_000_000
    assert u["por_tipo"][0]["tipo"] == "analise"
    assert len(u["por_dia"]) == 7
