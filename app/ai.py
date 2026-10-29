"""Motor do funcional: monta o contexto (thread, decisões, histórico, documentos) e chama o modelo."""
import json
import os
import re

import httpx

from .db import AIUsage, Analysis, Chunk, Decision, Document, Email, SessionLocal, now
from .retrieval import get_index, rank_texts, tokens as _tokens

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
11. Em "pedidos", separe item por item tudo o que a Motiva pede neste e-mail — uma frase curta por pedido —
    com urgência "alta" (prazo imediato, bloqueio ou crítica), "media" ou "baixa". Em "perguntas", liste
    TODAS as perguntas abertas que precisam de resposta, uma por item. O rascunho precisa cobrir cada pedido
    e responder cada pergunta dessas listas.
Estilo do rascunho: português, curto, direto, informal-profissional, sem parágrafos longos, sem listas com
hífen, sem jargão desnecessário, sem se justificar demais nem soar defensivo. Fecha com "Tks,".

Responda SOMENTE com um objeto JSON válido, sem texto antes ou depois, neste formato:
{
  "resumo": "o que a Motiva está dizendo/pedindo, em 2 a 4 frases",
  "classificacao": {"tipo": "sustentacao|melhoria|duvida|fora_do_escopo_lumos", "justificativa": "..."},
  "pedidos": [{"texto": "o que a Motiva pede, item por item", "urgencia": "alta|media|baixa"}],
  "perguntas": ["pergunta aberta da Motiva que precisa de resposta", "..."],
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


GUIDE_MAX = 14000
SPECIAL_TIPOS = ("regras", "guia_compras")   # documentos de referência com cartão próprio na Base


def guide_block(session):
    """Guia de Compras: entra em toda análise, chat e assistente para a IA entender a cadeia de compras."""
    g = session.query(Document).filter(Document.tipo == "guia_compras").order_by(Document.id.desc()).first()
    if not g or not (g.content or "").strip():
        return None, None
    txt = ("## GUIA DE COMPRAS (como funciona a cadeia de suprimentos da Motiva — use para entender o processo e "
           "explicar com propriedade; itens [CONFIRMAR] são hipótese, não regra acordada)\n"
           f"[DOC{g.id}] {g.nome}\n{(g.content or '')[:GUIDE_MAX]}")
    return txt, {"code": f"DOC{g.id}", "label": "Guia de Compras"}


def fmt_date(d):
    return d.strftime("%d/%m/%Y %H:%M") if d else "?"


TIPO_LABEL = {"regras": "Regras", "guia_compras": "Guia de Compras", "especificacao": "Especificação (EF)", "documentacao": "Documentação técnica",
              "whatsapp": "WhatsApp", "validador": "Validador de regras", "outro": "Documento"}


def enrich_sources(session, sources):
    """Torna as fontes legíveis: identifica o tipo de cada fonte (e-mail, documento, decisão)
    e monta um rótulo claro de onde a informação saiu — ex.: 'E-mail de 28/07 · Juliana: Cotação'."""
    em, doc, dec, rules = [], [], [], []
    for s_ in sources:
        code = s_.get("code", "")
        if code.startswith("E") and code[1:].isdigit():
            em.append((int(code[1:]), s_))
        elif code.startswith("DOC") and code[3:].isdigit():
            doc.append((int(code[3:]), s_))
        elif code.startswith("D") and code[1:].isdigit():
            dec.append((int(code[1:]), s_))
    if em:
        ids = [i for i, _ in em]
        rows = dict(session.query(Email.id, Email).filter(Email.id.in_(ids)).all())
        for eid, s_ in em:
            e = rows.get(eid)
            if not e:
                continue
            who = "StartGi (enviado)" if e.direction == "out" else e.from_addr
            s_["tipo"] = "email"
            s_["label"] = f"E-mail de {fmt_date(e.date)} · {who}: {e.subject}"
    if doc:
        ids = [i for i, _ in doc]
        rows = dict(session.query(Document.id, Document).filter(Document.id.in_(ids)).all())
        for did, s_ in doc:
            d = rows.get(did)
            if not d:
                continue
            s_["tipo"] = "documento"
            if d.tipo == "regras":
                s_["label"] = f"Regras do sistema: {d.nome}"
            elif d.tipo == "guia_compras":
                s_["label"] = "Guia de Compras"
            else:
                s_["label"] = f"Documento {TIPO_LABEL.get(d.tipo, d.tipo)}: {d.nome}"
    if dec:
        ids = [i for i, _ in dec]
        rows = dict(session.query(Decision.id, Decision).filter(Decision.id.in_(ids)).all())
        for did, s_ in dec:
            d = rows.get(did)
            if not d:
                continue
            s_["tipo"] = "decisao"
            s_["label"] = d.titulo
    return sources


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
    gtxt, gsrc = guide_block(session)
    if gtxt:
        parts.append(gtxt)
        sources.append(gsrc)

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
    return "\n\n".join(parts), enrich_sources(session, uniq)


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
    gtxt, gsrc = guide_block(session)
    if gtxt:
        parts.append(gtxt)
        sources.append(gsrc)

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
    return "\n\n".join(parts), enrich_sources(session, uniq)


def thread_chat(session, thread_key, question, history=None):
    """Responde à pergunta do funcional usando a cadeia como principal e o resto do sistema como apoio."""
    emails = session.query(Email).filter(Email.thread_key == thread_key).order_by(Email.date).all()
    if not emails:
        raise ValueError("Cadeia de e-mails não encontrada.")
    user, sources = build_thread_context(session, thread_key, question, history=history)
    raw = call_model(CHAT_THREAD_SYSTEM, user, kind="chat_conversa").strip()
    cited = set(re.findall(r"\[([A-Z]+?\d+)\]", raw))
    sources = [s for s in sources if s["code"] in cited]
    have = {s["code"] for s in sources}
    for e in emails:
        code = f"E{e.id}"
        if code in cited and code not in have:
            who = "StartGi (enviado)" if e.direction == "out" else e.from_addr
            sources.append({"code": code, "label": f"{fmt_date(e.date)} {who}"})
            have.add(code)
    return raw, enrich_sources(session, sources)


def call_model(system, user, kind="outro"):
    """Chama o modelo e registra falhas (chave inválida, timeout, resposta vazia) para o alerta do sistema."""
    try:
        return _call_model(system, user, kind)
    except Exception as ex:
        _record_usage(kind, None, ok=False, erro=str(ex))
        raise


def _call_model(system, user, kind):
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
        um = data.get("usageMetadata") or {}
        _record_usage(kind, (um.get("promptTokenCount"), um.get("candidatesTokenCount")))
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
        data = r.json()
        u = data.get("usage") or {}
        _record_usage(kind, (u.get("input_tokens"), u.get("output_tokens")))
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    url = (BASE_URL or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    r = httpx.post(url, timeout=TIMEOUT, headers={"Authorization": f"Bearer {API_KEY}"},
                   json={"model": MODEL, "max_tokens": MAX_TOKENS,
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    if r.status_code >= 400:
        raise http_error(r)
    data = r.json()
    u = data.get("usage") or {}
    _record_usage(kind, (u.get("prompt_tokens"), u.get("completion_tokens")))
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


def _record_usage(kind, toks, ok=True, erro=None):
    """Guarda quantos tokens cada chamada gastou (nunca derruba a chamada se falhar)."""
    try:
        tin, tout = toks or (0, 0)
        with SessionLocal() as s:
            s.add(AIUsage(tipo=kind, modelo=MODEL, tokens_entrada=int(tin or 0),
                          tokens_saida=int(tout or 0), ok=ok, erro=(erro or None) and erro[:1000]))
            s.commit()
    except Exception as ex:
        print(f"Falha ao registrar consumo da IA: {ex}", flush=True)


def health():
    """Estado da IA pelas últimas chamadas: ok, erro (com desde quando e a mensagem) ou sem_uso."""
    if not API_KEY:
        return {"estado": "nao_configurado", "mensagem": "Defina AI_API_KEY nas variáveis do Render."}
    with SessionLocal() as s:
        ult = s.query(AIUsage).order_by(AIUsage.id.desc()).limit(40).all()
    if not ult:
        return {"estado": "sem_uso"}
    if ult[0].ok:
        return {"estado": "ok", "ultima": ult[0].created_at.isoformat() + "Z"}
    falhas = []
    for u in ult:
        if u.ok:
            break
        falhas.append(u)
    return {"estado": "erro", "falhas": len(falhas), "desde": falhas[-1].created_at.isoformat() + "Z",
            "mensagem": explain_error(falhas[0].erro or ""), "detalhe": (falhas[0].erro or "")[:400]}


def explain_error(msg):
    """Traduz o erro técnico da IA para o que fazer."""
    low = (msg or "").lower()
    if "401" in low or "unauthorized" in low or "invalid" in low and "key" in low or "authorization" in low:
        return "A chave da IA foi recusada. Gere uma chave nova no provedor e atualize AI_API_KEY no Render."
    if "402" in low or "quota" in low or "insufficient" in low or "billing" in low or "credit" in low:
        return "A IA recusou por falta de crédito ou limite do plano. Confira o saldo no provedor."
    if "429" in low or "rate" in low:
        return "A IA está limitando o número de chamadas. Costuma passar sozinho em alguns minutos."
    if "timed out" in low or "timeout" in low:
        return "A IA demorou demais para responder. Se repetir, aumente AI_TIMEOUT no Render."
    if "404" in low or "model" in low and "not" in low:
        return "O modelo configurado não foi encontrado. Confira AI_MODEL no Render."
    if any(c in low for c in ("500", "502", "503", "504")):
        return "O provedor da IA está instável. Costuma voltar sozinho."
    return "A IA não está respondendo. Veja o detalhe abaixo."


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
        raw = call_model(SYSTEM, user, kind="analise")
        data = parse_json(raw)
        # uma análise ativa por e-mail: briefings antigos ainda não enviados são substituídos
        s.query(Analysis).filter(Analysis.email_id == email_id, Analysis.final_body.is_(None)) \
            .delete(synchronize_session=False)
        draft = data.get("rascunho") or {}
        a = Analysis(email_id=e.id, briefing_json=json.dumps(data, ensure_ascii=False),
                     draft_subject=draft.get("assunto") or f"RE: {e.subject}", draft_body=draft.get("corpo", ""),
                     model=f"{PROVIDER}:{MODEL}", sources_json=json.dumps(sources, ensure_ascii=False))
        s.add(a)
        ativas = s.query(Decision).filter(Decision.status.in_(ACTIVE_STATUSES)).all()
        for dsug in data.get("decisoes_sugeridas") or []:
            if dsug.get("regra"):
                parecida = find_similar_decision(ativas, dsug.get("titulo", ""), dsug.get("regra", ""))
                if parecida:
                    # já existe uma decisão sobre o mesmo assunto: registra a nova menção em vez de duplicar
                    nota = f"Também citada no e-mail E{e.id} de {fmt_date(e.date)}."
                    if nota not in (parecida.notas or ""):
                        parecida.notas = ((parecida.notas or "") + "\n" + nota).strip()
                    continue
                s.add(Decision(titulo=dsug.get("titulo", "")[:500], modulo=dsug.get("modulo", ""),
                               regra=dsug.get("regra"), fonte=dsug.get("fonte") or f"E-mail E{e.id} de {fmt_date(e.date)}",
                               data_decisao=e.date.strftime("%d/%m/%Y") if e.date else "",
                               status="proposta", origem_email_id=e.id))
        if e.status == "novo":
            e.status = "analisado"
        s.commit()
        return a.id


ACTIVE_STATUSES = ("proposta", "vigente", "em_discussao")


def _similarity(a, b):
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_similar_decision(decisions, titulo, regra, threshold=0.55):
    """Decisão já registrada sobre o mesmo assunto (título + regra parecidos), se houver."""
    best, best_sc = None, 0.0
    for d in decisions:
        sc = 0.4 * _similarity(titulo, d.titulo or "") + 0.6 * _similarity(regra, d.regra or "")
        if sc > best_sc:
            best, best_sc = d, sc
    return best if best_sc >= threshold else None


TRIAGE_SYSTEM = """Você revisa o registro de decisões do projeto Lumos (StartGi × Motiva).
Recebe UMA decisão que está "para revisar", o e-mail de onde ela saiu, os e-mails que vieram depois na mesma
conversa e outras decisões parecidas já registradas. Diga o que fazer com ela:
- "vigente": a regra foi confirmada/acordada e continua valendo.
- "descartada": o assunto foi encerrado, mudou, foi negado ou não era uma decisão de fato.
- "duplicada": já existe outra decisão registrada dizendo o mesmo (informe o número em "duplicada_de").
- "em_discussao": ainda não há acordo; o assunto continua em aberto.
Use só as fontes fornecidas e cite-as entre colchetes ([E#] e-mail, [D#] decisão). Sem evidência clara de
acordo, prefira "em_discussao" com confiança baixa. Responda SOMENTE com JSON:
{"acao": "vigente|descartada|duplicada|em_discussao", "duplicada_de": null, "confianca": "alta|media|baixa",
 "justificativa": "uma ou duas frases curtas, com citações"}"""

TRIAGE_ACTIONS = {"vigente", "descartada", "duplicada", "em_discussao"}


def review_decision(decision_id):
    """Pede à IA uma sugestão para uma decisão pendente e guarda em decision.sugestao_json."""
    with SessionLocal() as s:
        d = s.get(Decision, decision_id)
        if not d:
            raise ValueError("Decisão não encontrada.")
        parts = [f"## Decisão para revisar\n[D{d.id}] {d.modulo} | {d.titulo}\nRegra: {d.regra}\n"
                 f"Fonte: {d.fonte} | Data: {d.data_decisao}\nNotas: {d.notas or '-'}"]
        origem = s.get(Email, d.origem_email_id) if d.origem_email_id else None
        if origem:
            mails = (s.query(Email).filter(Email.thread_key == origem.thread_key)
                     .order_by(Email.date).all())
            depois = [m for m in mails if not origem.date or not m.date or m.date >= origem.date][-8:]
            parts.append("## E-mail de origem e o que veio depois na conversa")
            for m in depois:
                who = "StartGi (enviado)" if m.direction == "out" else m.from_addr
                parts.append(f"[E{m.id}] {fmt_date(m.date)} · {who} · {m.subject}\n"
                             f"{(m.body_clean or m.body or '')[:3000]}")
        else:
            parts.append("## E-mail de origem\n(não há e-mail de origem registrado)")
        outras = [x for x in s.query(Decision).filter(Decision.status.in_(ACTIVE_STATUSES),
                                                          Decision.id != d.id).all()]
        ranked = rank_texts(f"{d.titulo} {d.modulo} {d.regra}", outras,
                            key=lambda x: f"{x.titulo} {x.modulo} {x.regra}")
        parecidas = [x for sc, x in ranked if sc > 0][:8]
        parts.append("## Outras decisões parecidas já registradas")
        parts += [f"[D{x.id}] {x.status.upper()} | {x.modulo} | {x.titulo}\nRegra: {x.regra}" for x in parecidas] \
            or ["(nenhuma)"]
        raw = call_model(TRIAGE_SYSTEM, "\n\n".join(parts), kind="triagem")
        data = parse_json(raw)
        acao = str(data.get("acao") or "").strip().lower()
        if acao not in TRIAGE_ACTIONS:
            acao = "em_discussao"
        dup = data.get("duplicada_de")
        try:
            dup = int(str(dup).lstrip("Dd")) if dup not in (None, "", "null") else None
        except ValueError:
            dup = None
        if acao == "duplicada" and (not dup or dup == d.id or not s.get(Decision, dup)):
            acao, dup = "em_discussao", None
        sug = {"acao": acao, "duplicada_de": dup if acao == "duplicada" else None,
               "confianca": data.get("confianca") if data.get("confianca") in ("alta", "media", "baixa") else "baixa",
               "justificativa": str(data.get("justificativa") or "")[:1200]}
        d.sugestao_json = json.dumps(sug, ensure_ascii=False)
        d.sugestao_em = now()
        s.commit()
        return sug


def review_thread_decisions(email_id, limit=5):
    """E-mail novo numa conversa: reavalia as decisões ainda pendentes que saíram dessa conversa."""
    with SessionLocal() as s:
        e = s.get(Email, email_id)
        if not e:
            return 0
        ids = [i for (i,) in s.query(Email.id).filter(Email.thread_key == e.thread_key).all() if i != email_id]
        if not ids:
            return 0
        pend = [d.id for d in s.query(Decision).filter(Decision.status == "proposta",
                                                       Decision.origem_email_id.in_(ids))
                .order_by(Decision.id.desc()).limit(limit).all()]
    for did in pend:
        review_decision(did)
    return len(pend)


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
        new = call_model(REWRITE_SYSTEM, user, kind="reescrita").strip()
        a.draft_body = new
        s.commit()
        return new


def index_document(session, doc):
    from .db import Chunk
    from .retrieval import chunk_text, mark_dirty
    tags = f" [{doc.tags}]" if (doc.tags or "").strip() else ""
    session.query(Chunk).filter(Chunk.source_kind == "document", Chunk.source_id == doc.id).delete()
    for piece in chunk_text(doc.content, size=1500):
        session.add(Chunk(source_kind="document", source_id=doc.id,
                          label=f"{doc.tipo}: {doc.nome}{tags}", text=piece))
    mark_dirty()
