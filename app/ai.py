"""Motor do funcional: monta o contexto (thread, decisões, histórico, documentos) e chama o modelo."""
import json
import os
import re

import httpx

from .db import Analysis, Chunk, Decision, Document, Email, SessionLocal
from .retrieval import get_index, rank_texts

PROVIDER = os.getenv("AI_PROVIDER", "anthropic").lower()  # anthropic | openai | gemini
API_KEY = os.getenv("AI_API_KEY", "")
MODEL = os.getenv("AI_MODEL", "claude-sonnet-5")
BASE_URL = os.getenv("AI_BASE_URL", "")
MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS", "16000"))
TIMEOUT = float(os.getenv("AI_TIMEOUT", "180"))
GEMINI_DEFAULT_MODEL = "gemini-3.6-flash"

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
   não consta no documento de Regras do sistema, em nenhuma especificação ou decisão registrada é, em
   princípio, escopo novo.
7. Questione a regra do ponto de vista do processo de supply: se ela não faz sentido, gera ambiguidade ou
   conflita com outra regra, diga e sugira a pergunta certa a fazer à Motiva. Não seja passivo.
8. Dê uma recomendação única e firme. Não mude de posição sem um fato novo que justifique.
9. A base de conhecimento ([DOC#]) traz as especificações e documentações oficiais anexadas ao sistema.
   Sempre que o contexto trouxer trecho [DOC#] sobre o assunto do e-mail, use o documento como fonte da
   regra, cite [DOC#] no briefing e no rascunho, e trate o que o documento diz como prioridade sobre a sua própria interpretação.
   O índice [DOCid] lista TODOS os documentos da base: se algum do índice parecer relevante para o pedido
   mas não tiver trecho no contexto, inclua em "validar_antes_de_enviar" a checagem desse documento
   (ex.: "conferir regra na EF-12"). Se nenhum documento tratar do assunto, diga isso em "historico_relevante".
10. O documento "Regras do sistema" (seção REGRAS DO SISTEMA) é a referência para validar se uma regra
    consta no sistema. Sempre que o e-mail citar, pressupor ou questionar uma regra, confere nele e registra
    em "historico_relevante" se a regra CONSTA (com citação [DOC#]) ou NÃO CONSTA — isso alimenta a
    classificação da regra 6.
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


def search_docs(session, idx, query, exclude=None, k=12, min_hits=4):
    """Busca na base de conhecimento: BM25 com no máximo 3 trechos por documento e,
    se a busca acertar poucos, completa com o início dos documentos restantes (mais recentes
    primeiro). Garante que o briefing sempre veja a base quando ela existe."""
    hits = idx.search(query, k=k * 3, exclude=exclude, kinds={"document"}) if idx else []
    chosen, per = [], {}
    for h in hits:
        sid = h[1][2]
        if per.get(sid, 0) >= 3:
            continue
        chosen.append(h)
        per[sid] = per.get(sid, 0) + 1
        if len(chosen) >= k:
            break
    if len(chosen) < min_hits:
        have = {h[1][2] for h in chosen}
        for (did,) in session.query(Document.id).order_by(Document.id.desc()).all():
            if len(chosen) >= min_hits + 2:
                break
            if did in have:
                continue
            c = session.query(Chunk).filter(Chunk.source_kind == "document",
                                             Chunk.source_id == did).order_by(Chunk.id).first()
            if c:
                chosen.append((0.0, (c.id, "document", did, c.label, c.text)))
                have.add(did)
    return chosen


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

    rules = session.query(Document).filter(Document.tipo == "regras").order_by(Document.id.desc()).first()
    if rules and (rules.content or "").strip():
        parts.append("## REGRAS DO SISTEMA (documento de referência — valide aqui se a regra CONSTA ou NÃO CONSTA)")
        parts.append(f"[DOC{rules.id}] {rules.nome}\n{(rules.content or '')[:8000]}")
        sources.append({"code": f"DOC{rules.id}", "label": "Regras do sistema"})

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
    hits_docs = search_docs(session, idx, query, exclude=thread_ids)
    hits_mail = idx.search(query, k=10, exclude=thread_ids, kinds={"email"})
    docs_all = session.query(Document.id, Document.tipo, Document.nome).order_by(Document.id.desc()).all()
    parts.append("## Índice da base de conhecimento (todos os documentos anexados)")
    if docs_all:
        for did, tipo, nome in docs_all:
            parts.append(f"[DOC{did}] {tipo}: {nome}")
    else:
        parts.append("(nenhum documento anexado à base de conhecimento)")
    parts.append("## Trechos dos documentos relacionados a este e-mail")
    if not hits_docs:
        parts.append("(nenhum trecho relacionado)")
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


CHAT_THREAD_SYSTEM = """Você é o assistente do funcional sênior da StartGi no projeto Lumos (cliente Motiva) e conversa
com o funcional SOBRE UMA CADEIA DE E-MAILS específica.

Hierarquia das fontes:
1. A CADEIA DE E-MAILS abaixo é o contexto PRINCIPAL: é o que já foi pedido, dito e respondido entre a Motiva e
   a StartGi. Nunca contradiga o que está na cadeia.
2. O registro de decisões [D#], a base de conhecimento [DOC#] e outros e-mails do projeto [E#] dão apoio e
   contexto — use-os para responder com segurança; se conflitarem com a cadeia, aponte o conflito.

Regras:
1. Responda em português, direto e curto. Responda dúvidas do funcional sobre o projeto (regras, histórico,
   decisões, o que já foi dito, o que ainda está em aberto) usando as fontes.
2. Cite as fontes entre colchetes: [E#] cadeia e outros e-mails, [D#] decisão, [DOC#] documento.
3. Nunca invente. Sem fonte, diga que não há informação registrada — se for essencial, sugira o que pesquisar
   ou anexar na base.
4. Diferencie o que está DITO na cadeia do que é inferência sua.
5. Se pedirem um rascunho de resposta à Motiva: português, curto, direto, sem listas com hífen, fecha com "Tks,".
6. Para dizer se uma regra consta ou não no sistema, confira primeiro o documento "Regras do sistema"
   (seção REGRAS DO SISTEMA) e responda explicitamente "consta" ou "não consta", citando [DOC#]."""


def build_thread_context(session, thread_key, question, history=None):
    """Contexto do chat por cadeia: a cadeia é o principal; decisões, docs e outros e-mails dão apoio."""
    emails = session.query(Email).filter(Email.thread_key == thread_key).order_by(Email.date).all()
    if not emails:
        raise ValueError("Cadeia de e-mails não encontrada.")
    thread_ids = {("email", e.id) for e in emails}
    parts, sources = [], []

    parts.append("## CADEIA DE E-MAILS (contexto principal, ordem cronológica)")
    for e in emails:
        who = "StartGi (enviado)" if e.direction == "out" else e.from_addr
        parts.append(f"[E{e.id}] {fmt_date(e.date)} | De: {who} | Assunto: {e.subject}\n"
                     f"{(e.body_clean or e.body or '')[:6000]}")

    rules = session.query(Document).filter(Document.tipo == "regras").order_by(Document.id.desc()).first()
    if rules and (rules.content or "").strip():
        parts.append("## REGRAS DO SISTEMA (documento de referência — valide aqui se a regra CONSTA ou NÃO CONSTA)")
        parts.append(f"[DOC{rules.id}] {rules.nome}\n{(rules.content or '')[:8000]}")
        sources.append({"code": f"DOC{rules.id}", "label": "Regras do sistema"})

    subjects = " ".join(dict.fromkeys(e.subject for e in emails if e.subject))
    query = f"{subjects}\n{question}"

    decisions = session.query(Decision).filter(Decision.status.in_(["vigente", "em_discussao", "substituida"])).all()
    ranked = rank_texts(query, decisions, key=lambda d: f"{d.titulo} {d.modulo} {d.regra}")
    chosen = [d for sc, d in ranked if sc > 0][:25] or [d for d in decisions if d.status == "vigente"][:10]
    parts.append("## Registro de decisões (apoio)")
    if not chosen:
        parts.append("(nenhuma decisão registrada)")
    for d in chosen:
        extra = f" (substituída por D{d.substituida_por})" if d.substituida_por else ""
        parts.append(f"[D{d.id}] {d.status.upper()}{extra} | {d.modulo} | {d.titulo}\nRegra: {d.regra}")
        sources.append({"code": f"D{d.id}", "label": d.titulo})

    idx = get_index()
    hits_docs = search_docs(session, idx, query, exclude=thread_ids)
    hits_mail = idx.search(query, k=10, exclude=thread_ids, kinds={"email"}) if idx else []
    docs_all = session.query(Document.id, Document.tipo, Document.nome).order_by(Document.id.desc()).all()
    parts.append("## Índice da base de conhecimento (todos os documentos anexados)")
    if docs_all:
        for did, tipo, nome in docs_all:
            parts.append(f"[DOC{did}] {tipo}: {nome}")
    else:
        parts.append("(nenhum documento anexado à base de conhecimento)")
    parts.append("## Trechos de documentos (apoio)")
    if not hits_docs:
        parts.append("(nenhum trecho relacionado)")
    for sc, (cid, kind, sid, label, text) in hits_docs:
        parts.append(f"[DOC{sid}] {label}\n{text}")
        sources.append({"code": f"DOC{sid}", "label": label})
    parts.append("## Outros e-mails do projeto (apoio, fora desta cadeia)")
    for sc, (cid, kind, sid, label, text) in hits_mail:
        parts.append(f"[E{sid}] {label}\n{text}")
        sources.append({"code": f"E{sid}", "label": label})

    if history:
        parts.append("## Histórico desta conversa com o funcional")
        for m in history[-16:]:
            who = "Funcional" if m.role == "user" else "Assistente"
            parts.append(f"{who}: {(m.content or '')[:1500]}")

    parts.append("## Nova pergunta do funcional")
    parts.append(question)

    seen, uniq = set(), []
    for s_ in sources:
        if s_["code"] not in seen:
            seen.add(s_["code"])
            uniq.append(s_)
    return "\n\n".join(parts), uniq


def thread_chat(session, thread_key, question, history=None):
    """Responde à pergunta do funcional usando a cadeia como principal e o resto do sistema como apoio."""
    emails = session.query(Email).filter(Email.thread_key == thread_key).order_by(Email.date).all()
    if not emails:
        raise ValueError("Cadeia de e-mails não encontrada.")
    user, sources = build_thread_context(session, thread_key, question, history=history)
    raw = call_model(CHAT_THREAD_SYSTEM, user).strip()
    cited = set(re.findall(r"\[([A-Z]+?\d+)\]", raw))
    sources = [s for s in sources if s["code"] in cited]
    have = {s["code"] for s in sources}
    for e in emails:
        code = f"E{e.id}"
        if code in cited and code not in have:
            who = "StartGi (enviado)" if e.direction == "out" else e.from_addr
            sources.append({"code": code, "label": f"{fmt_date(e.date)} {who}"})
            have.add(code)
    return raw, sources


def call_model(system, user):
    if not API_KEY:
        raise RuntimeError("IA não configurada: defina AI_API_KEY (e AI_PROVIDER/AI_MODEL).")
    use_gemini = PROVIDER in ("gemini", "google") or (
        PROVIDER == "openai" and "generativelanguage.googleapis.com" in (BASE_URL or ""))
    if use_gemini:
        base = (BASE_URL or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        if base.endswith("/openai"):
            base = base[: -len("/openai")]
        url = f"{base}/models/{MODEL or GEMINI_DEFAULT_MODEL}:generateContent"
        r = httpx.post(url, timeout=TIMEOUT, headers={
            "x-goog-api-key": API_KEY, "content-type": "application/json"},
            json={"systemInstruction": {"parts": [{"text": system}]},
                  "contents": [{"role": "user", "parts": [{"text": user}]}],
                  "generationConfig": {"maxOutputTokens": MAX_TOKENS}})
        if r.status_code >= 400:
            raise http_error(r)
        data = r.json()
        try:
            return "".join(p.get("text", "") for c in data.get("candidates", [])
                           for p in (c.get("content") or {}).get("parts", [])).strip()
        except Exception:
            raise RuntimeError(f"Resposta inesperada do Gemini: {str(data)[:500]}")
    if PROVIDER == "anthropic":
        url = (BASE_URL or "https://api.anthropic.com") + "/v1/messages"
        r = httpx.post(url, timeout=TIMEOUT, headers={
            "x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": MAX_TOKENS, "system": system,
                  "messages": [{"role": "user", "content": user}]})
        if r.status_code >= 400:
            raise http_error(r)
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    url = (BASE_URL or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    r = httpx.post(url, timeout=TIMEOUT, headers={"Authorization": f"Bearer {API_KEY}"},
                   json={"model": MODEL, "max_tokens": MAX_TOKENS,
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    if r.status_code >= 400:
        raise http_error(r)
    data = r.json()
    try:
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Resposta inesperada da IA: {str(data)[:400]}")
    if not content.strip():
        finish = choice.get("finish_reason") or "?"
        raise RuntimeError(
            f"A IA retornou conteúdo vazio (finish_reason={finish}, modelo={MODEL}). "
            f"Resposta: {str(data)[:400]}")
    return content


def http_error(r):
    detail = (r.text or "").strip()[:500].replace("\n", " ")
    msg = f"IA respondeu HTTP {r.status_code} para {r.url}"
    return RuntimeError(f"{msg}: {detail}" if detail else msg)


def parse_json(text):
    raw = (text or "").strip()
    text = re.sub(r"^```(json)?|```$", "", raw, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"A IA não retornou JSON. Resposta recebida: {raw[:400]}")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as ex:
        raise ValueError(f"A IA retornou JSON inválido ({ex}). Trecho: {text[start:start + 400]}")


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
