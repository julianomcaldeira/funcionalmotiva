"""Motor do funcional: monta o contexto (thread, decisões, histórico, documentos) e chama o modelo."""
import json
import os
import re

import httpx

from .db import Analysis, Decision, Document, Email, SessionLocal
from .retrieval import get_index, rank_texts

PROVIDER = os.getenv("AI_PROVIDER", "anthropic").lower()  # anthropic | openai (qualquer API compatível)
API_KEY = os.getenv("AI_API_KEY", "")
MODEL = os.getenv("AI_MODEL", "claude-sonnet-5")
BASE_URL = os.getenv("AI_BASE_URL", "")
MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "6000"))
TIMEOUT = float(os.getenv("AI_TIMEOUT", "180"))

SYSTEM = """Você é o consultor funcional sênior da StartGi no projeto Lumos, cliente Motiva (antigo Grupo CCR).
Você domina o processo de supply chain e procurement: requisição, sourcing (RFx, carta convite, estratégia de
sourcing e sua cadeia de aprovação), tabela comparativa e equalização, adjudicação, pedido, contrato (Netlex,
DocuSign), integração com SAP (MM) e Coupa. O Lumos é a plataforma da StartGi que recebe dados de SAP, Coupa,
Netlex e DocuSign e monta painéis, tracking de etapas, LOF e Tabela Comparativa. O Lumos é receptor: ele não
altera dados dos sistemas de origem.

Seu trabalho, a cada e-mail da Motiva, é preparar o Juliano (CEO da StartGi) para responder bem. Regras:

1. Nunca invente. Toda afirmação sobre regra, acordo ou histórico precisa vir das fontes fornecidas, citadas
   pelo código entre colchetes, ex.: [D12], [E345], [DOC7]. Se não houver fonte, diga que não há.
2. O registro de decisões (códigos D) é a referência principal. Decisões "vigentes" valem sobre e-mails
   antigos. Se o e-mail da Motiva contraria uma decisão vigente, aponte isso.
3. Verifique a consistência com o que a StartGi já afirmou (e-mails enviados pela StartGi). Se o rascunho
   contradisser algo já dito, aponte em "contradicoes" e ajuste o rascunho. Nunca use um argumento que o
   próprio histórico desmente.
4. Não afirme como fato nada que ainda não foi testado. Se a resposta depende de um teste (validador de regras
   do Lumos, chamada à API do Coupa, consulta ao SAP), liste em "validar_antes_de_enviar" e escreva o
   rascunho de forma que não se comprometa com o resultado. Se o teste for essencial, recomende segurar o envio.
5. Separe o que é do Lumos do que é de sistema externo (Coupa, SAP, conector, configuração de aprovação).
   Defenda a StartGi com argumentos legítimos e documentados, nunca com afirmações falsas.
6. Classifique o pedido: "sustentacao" (correção dentro do que foi especificado, coberto pela mensalidade),
   "melhoria" (regra, visão ou escopo novo, cotado à parte), "duvida" ou "fora_do_escopo_lumos". Regra que
   não consta em nenhuma especificação ou decisão registrada é, em princípio, escopo novo.
7. Questione a regra do ponto de vista do processo de supply: se ela não faz sentido, gera ambiguidade ou
   conflita com outra regra, diga e sugira a pergunta certa a fazer à Motiva. Não seja passivo.
8. Dê uma recomendação única e firme. Não mude de posição sem um fato novo que justifique.

Estilo do rascunho: português, curto, direto, informal-profissional, sem parágrafos longos, sem listas com
hífen, sem jargão desnecessário, sem se justificar demais nem soar defensivo. Fecha com "Tks,".

Responda SOMENTE com um objeto JSON válido, sem texto antes ou depois, neste formato:
{
  "resumo": "o que a Motiva está dizendo/pedindo, em 2 a 4 frases",
  "classificacao": {"tipo": "sustentacao|melhoria|duvida|fora_do_escopo_lumos", "justificativa": "..."},
  "historico_relevante": ["fato com citação [..]", "..."],
  "contradicoes": ["contradição encontrada, com citações", "..."],
  "questionamentos_de_processo": ["o que não faz sentido na regra ou precisa ser perguntado", "..."],
  "validar_antes_de_enviar": ["teste/checagem concreta, com o que conferir", "..."],
  "riscos": ["risco para a StartGi ao responder", "..."],
  "recomendacao": "a recomendação única: responder agora, testar antes, escalar, etc.",
  "confianca": "alta|media|baixa",
  "rascunho": {"assunto": "RE: ...", "corpo": "..."},
  "decisoes_sugeridas": [{"titulo": "...", "modulo": "...", "regra": "...", "fonte": "este e-mail ou [..]"}]
}
Em "decisoes_sugeridas", inclua somente regras ou acordos que este e-mail formaliza de fato (não suposições)."""


def fmt_date(d):
    return d.strftime("%d/%m/%Y %H:%M") if d else "?"


def build_context(session, email_obj):
    thread = session.query(Email).filter(Email.thread_key == email_obj.thread_key).order_by(Email.date).all()
    thread_ids = {("email", e.id) for e in thread}
    parts, sources = [], []

    parts.append("## Thread atual (ordem cronológica)")
    for e in thread:
        who = "StartGi (enviado)" if e.direction == "out" else e.from_addr
        mark = "  <<< E-MAIL A RESPONDER" if e.id == email_obj.id else ""
        parts.append(f"[E{e.id}] {fmt_date(e.date)} | De: {who} | Assunto: {e.subject}{mark}\n{(e.body_clean or e.body or '')[:6000]}")
        sources.append({"code": f"E{e.id}", "label": f"{fmt_date(e.date)} {who}"})

    query = f"{email_obj.subject}\n{(email_obj.body_clean or email_obj.body or '')[:3000]}"

    decisions = session.query(Decision).filter(Decision.status.in_(["vigente", "em_discussao", "substituida"])).all()
    ranked = rank_texts(query, decisions, key=lambda d: f"{d.titulo} {d.modulo} {d.regra}")
    chosen = [d for sc, d in ranked if sc > 0][:25] or [d for d in decisions if d.status == "vigente"][:10]
    parts.append("## Registro de decisões (fonte principal)")
    if not chosen:
        parts.append("(nenhuma decisão registrada ainda relacionada a este assunto)")
    for d in chosen:
        extra = f" (substituída por D{d.substituida_por})" if d.substituida_por else ""
        parts.append(f"[D{d.id}] {d.status.upper()}{extra} | {d.modulo} | {d.titulo}\nRegra: {d.regra}\n"
                     f"Fonte: {d.fonte} | Data: {d.data_decisao} | Aprovado por: {d.aprovado_por}")
        sources.append({"code": f"D{d.id}", "label": d.titulo})

    idx = get_index()
    hits_docs = idx.search(query, k=8, exclude=thread_ids, kinds={"document"})
    hits_mail = idx.search(query, k=10, exclude=thread_ids, kinds={"email"})
    parts.append("## Documentos e especificações relacionados")
    for sc, (cid, kind, sid, label, text) in hits_docs:
        parts.append(f"[DOC{sid}] {label}\n{text}")
        sources.append({"code": f"DOC{sid}", "label": label})
    parts.append("## Outros e-mails relacionados (inclui o que a StartGi já afirmou)")
    for sc, (cid, kind, sid, label, text) in hits_mail:
        parts.append(f"[E{sid}] {label}\n{text}")
        sources.append({"code": f"E{sid}", "label": label})

    # aprendizado de estilo: respostas finais que o Juliano de fato enviou
    finals = session.query(Analysis).filter(Analysis.final_body.isnot(None)).order_by(Analysis.id.desc()).limit(3).all()
    if finals:
        parts.append("## Exemplos de respostas que o Juliano aprovou e enviou (siga o estilo)")
        for a in finals:
            parts.append(a.final_body[:1500])

    seen, uniq = set(), []
    for s_ in sources:
        if s_["code"] not in seen:
            seen.add(s_["code"])
            uniq.append(s_)
    return "\n\n".join(parts), uniq


def call_model(system, user):
    if not API_KEY:
        raise RuntimeError("IA não configurada: defina AI_API_KEY (e AI_PROVIDER/AI_MODEL).")
    if PROVIDER == "anthropic":
        url = (BASE_URL or "https://api.anthropic.com") + "/v1/messages"
        r = httpx.post(url, timeout=TIMEOUT, headers={
            "x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": MAX_TOKENS, "system": system,
                  "messages": [{"role": "user", "content": user}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    url = (BASE_URL or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    r = httpx.post(url, timeout=TIMEOUT, headers={"Authorization": f"Bearer {API_KEY}"},
                   json={"model": MODEL, "max_tokens": MAX_TOKENS,
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def parse_json(text):
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("A IA não retornou JSON.")
    return json.loads(text[start:end + 1])


def analyze(email_id):
    with SessionLocal() as s:
        e = s.get(Email, email_id)
        if not e:
            raise ValueError("E-mail não encontrado.")
        context, sources = build_context(s, e)
        user = f"{context}\n\n## Tarefa\nPrepare o briefing e o rascunho de resposta para o e-mail [E{e.id}]."
        raw = call_model(SYSTEM, user)
        data = parse_json(raw)
        draft = data.get("rascunho") or {}
        a = Analysis(email_id=e.id, briefing_json=json.dumps(data, ensure_ascii=False),
                     draft_subject=draft.get("assunto") or f"RE: {e.subject}", draft_body=draft.get("corpo", ""),
                     model=f"{PROVIDER}:{MODEL}", sources_json=json.dumps(sources, ensure_ascii=False))
        s.add(a)
        for dsug in data.get("decisoes_sugeridas") or []:
            if dsug.get("regra"):
                s.add(Decision(titulo=dsug.get("titulo", "")[:500], modulo=dsug.get("modulo", ""),
                               regra=dsug.get("regra"), fonte=dsug.get("fonte") or f"E-mail E{e.id} de {fmt_date(e.date)}",
                               data_decisao=e.date.strftime("%d/%m/%Y") if e.date else "",
                               status="proposta", origem_email_id=e.id))
        if e.status == "novo":
            e.status = "analisado"
        s.commit()
        return a.id


REWRITE_SYSTEM = """Você reescreve rascunhos de e-mail do Juliano (CEO da StartGi) para a Motiva.
Aplique exatamente a instrução pedida, mantendo os fatos do rascunho e do briefing. Não acrescente
afirmações sem fonte. Estilo: curto, direto, sem listas com hífen, fecha com "Tks,".
Responda SOMENTE com o novo corpo do e-mail, sem comentários."""


def rewrite(analysis_id, instruction, current_body=None):
    with SessionLocal() as s:
        a = s.get(Analysis, analysis_id)
        body = current_body or a.draft_body
        user = (f"## Briefing\n{a.briefing_json}\n\n## E-mail recebido\n{a.email.body_clean or a.email.body}\n\n"
                f"## Rascunho atual\n{body}\n\n## Instrução\n{instruction}")
        new = call_model(REWRITE_SYSTEM, user).strip()
        a.draft_body = new
        s.commit()
        return new


def index_document(session, doc):
    from .db import Chunk
    from .retrieval import chunk_text, mark_dirty
    session.query(Chunk).filter(Chunk.source_kind == "document", Chunk.source_id == doc.id).delete()
    for piece in chunk_text(doc.content, size=1500):
        session.add(Chunk(source_kind="document", source_id=doc.id, label=f"{doc.tipo}: {doc.nome}", text=piece))
    mark_dirty()
