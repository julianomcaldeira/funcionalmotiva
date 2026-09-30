from app.extract import extract_text_from_bytes
from app.main import fold, make_snippet
from app.mail import html_to_text, is_motiva, normalize_subject, strip_quoted
from app.retrieval import rank_texts


def test_fold():
    assert fold("Çoupa É melhor ÁREA") == "coupa e melhor area"
    assert fold("") == ""


def test_make_snippet():
    texto = ("Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore "
             "et dolore magna aliqua Ut enim ad minim veniam quis nostrud exercitation ullamco laboris nisi ut "
             "coupa aliquip ex ea commodo consequat duis aute irure dolor in reprehenderit in voluptate velit "
             "esse cillum dolore eu fugiat nulla pariatur excepteur sint occaecat cupidatat non proident")
    s = make_snippet(texto, "coupa")
    assert "coupa" in s and "…" in s


def test_normalize_subject():
    assert normalize_subject("Re: Fw: Tracking de Etapas") == "tracking de etapas"
    assert normalize_subject("   ").startswith("(")


def test_strip_quoted():
    body = "Conteúdo principal.\nEm 29/09/2026, Juliana escreveu:\n> resposta citada."
    out = strip_quoted(body)
    assert "resposta citada" not in out
    assert "Conteúdo principal" in out


def test_html_to_text():
    html = "<p>Olá<br>mundo</p><script>var x = 1;</script>"
    out = html_to_text(html)
    assert "Olá" in out and "mundo" in out and "var x" not in out


def test_is_motiva():
    assert is_motiva(["alguem@motiva.com.br"])
    assert is_motiva(["joao@grupo ccr.com.br".replace(" ", "")])
    assert not is_motiva(["outro@gmail.com"])


def test_extract_txt():
    assert extract_text_from_bytes("a.txt", "olá".encode("utf-8")) == "olá"
    assert extract_text_from_bytes("a.xyz", b"x") == ""


def test_rank_texts():
    items = [{"t": "planilha receita", "v": 2}, {"t": "Lumos integra sap coupa", "v": 1}]
    scored = rank_texts("coupa", items, key=lambda i: i["t"])
    assert scored[0][1]["t"] == "Lumos integra sap coupa"
    assert scored[1][1]["t"] == "planilha receita"