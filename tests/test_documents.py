def upload(client, texto="Conteúdo da v1", nome="EF-Coupa-004", tipo="especificacao"):
    r = client.post("/api/documents", data={"tipo": tipo, "nome": nome, "texto": texto, "tags": "coupa"})
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_cria_baseline_v1(client):
    d = upload(client)
    assert d["versao"] == 1
    lst = client.get("/api/documents").json()
    row = next(x for x in lst if x["id"] == d["id"])
    assert row["versao"] == 1
    vs = client.get(f"/api/documents/{d['id']}/versions").json()
    assert vs["versao_atual"] == 1
    assert len(vs["versoes"]) == 1
    assert vs["versoes"][0]["versao"] == 1
    assert vs["versoes"][0]["nota"] == "Versão inicial"


def test_texto_sem_conteudo_e_regras_bloqueadas(client):
    assert client.post("/api/documents", data={"tipo": "outro", "nome": "X", "texto": "  "}).status_code == 400
    assert client.post("/api/documents", data={"tipo": "regras", "nome": "R", "texto": "regra"}).status_code == 400


def test_nova_versao_uma_linha_por_versao(client):
    d = upload(client)
    r = client.post(f"/api/documents/{d['id']}/versions",
                    data={"texto": "Conteúdo da v2 corrigido §3.2", "nota": "Corrigido §3.2"})
    assert r.status_code == 200, r.text
    assert r.json()["versao"] == 2

    # v1 e v2 exatamente uma vez cada (baseline não é duplicada)
    vs = client.get(f"/api/documents/{d['id']}/versions").json()
    assert vs["versao_atual"] == 2
    assert [v["versao"] for v in vs["versoes"]] == [1, 2]
    assert vs["versoes"][1]["nota"] == "Corrigido §3.2"

    # o documento vigente passa a ser o novo conteúdo e aparece a badge na listagem
    doc = client.get(f"/api/documents/{d['id']}").json()
    assert doc["content"] == "Conteúdo da v2 corrigido §3.2"
    assert doc["versao"] == 2
    lst = client.get("/api/documents").json()
    assert next(x for x in lst if x["id"] == d["id"])["versao"] == 2


def test_texto_de_versao_e_dados_de_v1(client):
    d = upload(client, texto="texto original")
    client.post(f"/api/documents/{d['id']}/versions", data={"texto": "texto novo", "nota": "v2"})
    v1 = client.get(f"/api/documents/{d['id']}/versions/1").json()
    assert v1["content"] == "texto original"
    v2 = client.get(f"/api/documents/{d['id']}/versions/2").json()
    assert v2["content"] == "texto novo"
    assert client.get(f"/api/documents/{d['id']}/versions/99").status_code == 404


def test_nova_versao_precisa_de_conteudo(client):
    d = upload(client)
    assert client.post(f"/api/documents/{d['id']}/versions",
                       data={"texto": "", "nota": "x"}).status_code == 400
    assert client.post("/api/documents/999/versions", data={"texto": "t"}).status_code == 404


def test_regras_nao_tem_versao(client):
    r = client.put("/api/regras", json={"content": "Sistema é receptor."})
    assert r.status_code == 200
    rid = r.json()["id"]
    assert client.post(f"/api/documents/{rid}/versions", data={"texto": "v"}).status_code == 400


def test_upload_arquivo_txt(client):
    r = client.post("/api/documents", data={"tipo": "documentacao", "nome": "meu.txt"},
                    files={"arquivo": ("meu.txt", b"linha um\nlinha dois", "text/plain")})
    assert r.status_code == 200, r.text
    doc = client.get(f"/api/documents/{r.json()['id']}").json()
    assert "linha dois" in doc["content"]
    assert doc["tem_arquivo"] is True