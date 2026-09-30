from .conftest import make_decision


def test_create_and_list(client):
    r = client.post("/api/decisions", json=make_decision({"modulo": "Coupa"}))
    assert r.status_code == 200, r.text
    did = r.json()["id"]
    lst = client.get("/api/decisions").json()
    assert any(d["id"] == did and d["modulo"] == "Coupa" for d in lst)
    filt = client.get("/api/decisions", params={"status": "vigente"}).json()
    assert any(d["id"] == did for d in filt)
    assert client.get("/api/decisions", params={"status": "descartada"}).json() == []


def test_edit_archives_revision(client):
    did = client.post("/api/decisions", json=make_decision()).json()["id"]
    r = client.put(f"/api/decisions/{did}", json=make_decision({
        "regra": "Nova regra: exibir após nota fiscal.", "fonte": "Reunião de 05/08"}))
    assert r.status_code == 200
    assert r.json()["regra"] == "Nova regra: exibir após nota fiscal."

    revs = client.get(f"/api/decisions/{did}/revisions").json()
    assert len(revs) == 1
    assert revs[0]["regra"] == "Exibir a etapa conforme aprovado no Coupa."
    assert revs[0]["status"] == "vigente"

    # revisão nova não cria revisão da própria criação, e voltar a editar adiciona outra
    client.put(f"/api/decisions/{did}", json=make_decision({"status": "proposta"}))
    assert len(client.get(f"/api/decisions/{did}/revisions").json()) == 2


def test_restore_revision(client):
    did = client.post("/api/decisions", json=make_decision()).json()["id"]
    client.put(f"/api/decisions/{did}", json=make_decision({"regra": "regra v2"}))
    revs = client.get(f"/api/decisions/{did}/revisions").json()
    r = client.put(f"/api/decisions/{did}", json={
        "modulo": revs[0]["modulo"], "titulo": revs[0]["titulo"], "regra": revs[0]["regra"],
        "fonte": revs[0]["fonte"], "data_decisao": revs[0]["data_decisao"],
        "aprovado_por": revs[0]["aprovado_por"], "status": revs[0]["status"],
        "substituida_por": revs[0]["substituida_por"], "notas": revs[0]["notas"]})
    assert r.status_code == 200
    assert r.json()["regra"] == "Exibir a etapa conforme aprovado no Coupa."
    assert len(client.get(f"/api/decisions/{did}/revisions").json()) == 2


def test_validation_errors(client):
    assert client.post("/api/decisions", json=make_decision({"status": "inexistente"})).status_code == 400
    did = client.post("/api/decisions", json=make_decision()).json()["id"]
    assert client.put(f"/api/decisions/{did}", json=make_decision({"substituida_por": 999})).status_code == 400
    assert client.put(f"/api/decisions/{did}", json=make_decision({"substituida_por": did})).status_code == 400
    assert client.put("/api/decisions/999", json=make_decision()).status_code == 404
    assert client.get("/api/decisions/999/revisions").status_code == 404


def test_substituida_marca_status(client):
    a = client.post("/api/decisions", json=make_decision()).json()["id"]
    b = client.post("/api/decisions", json=make_decision({"titulo": "Decisão B"})).json()["id"]
    r = client.put(f"/api/decisions/{a}", json=make_decision({"substituida_por": b}))
    assert r.status_code == 200
    assert r.json()["status"] == "substituida"
    assert r.json()["substituida_por"] == b