import app.ai as ai


def test_requer_ia_configurada(client, monkeypatch):
    monkeypatch.setattr(ai, "API_KEY", "")
    r = client.post("/api/chat", json={"pergunta": "oi"})
    assert r.status_code == 400
    assert client.post("/api/chat", json={"pergunta": "   "}).status_code == 400


def test_conversa_cria_sessao_e_historico(client, monkeypatch):
    monkeypatch.setattr(ai, "API_KEY", "test")
    monkeypatch.setattr(ai, "call_model", lambda system, user: "Resposta de teste.")
    monkeypatch.setattr(ai, "search_docs", lambda *a, **k: [])

    r = client.post("/api/chat", json={"pergunta": "O que é o Lumos?"})
    assert r.status_code == 200, r.text
    data = r.json()
    sid = data["session_id"]
    assert data["resposta"] == "Resposta de teste."
    assert isinstance(data["fontes"], list)

    sessions = client.get("/api/chat/sessions").json()
    assert len(sessions) == 1 and sessions[0]["n"] == 2

    hist = client.get(f"/api/chat/sessions/{sid}").json()
    assert [m["role"] for m in hist["messages"]] == ["user", "assistant"]
    assert hist["messages"][0]["content"] == "O que é o Lumos?"

    # continuação na mesma conversa
    client.post("/api/chat", json={"pergunta": "E sobre o tracking?", "session_id": sid})
    hist = client.get(f"/api/chat/sessions/{sid}").json()
    assert len(hist["messages"]) == 4
    assert client.get("/api/chat/sessions").json()[0]["n"] == 4

    # exclusão
    assert client.delete(f"/api/chat/sessions/{sid}").status_code == 200
    assert client.get("/api/chat/sessions").json() == []
    assert client.get(f"/api/chat/sessions/{sid}").status_code == 404