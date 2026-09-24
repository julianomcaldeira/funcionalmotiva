# Funcional Lumos

Sistema interno da StartGi que lê os e-mails da Motiva no Zoho, cruza com o histórico do projeto e prepara, para cada e-mail, um briefing de funcional sênior e um rascunho de resposta. Nada é enviado pelo sistema: a resposta final é sempre revisada e enviada por uma pessoa, pelo Zoho.

## O que ele faz

A cada e-mail da Motiva, o sistema monta o contexto (a conversa inteira, o registro de decisões, documentos e especificações, e o que a StartGi já afirmou em outros e-mails) e entrega a classificação do pedido (sustentação, melhoria, dúvida ou fora do escopo do Lumos), a recomendação, o que precisa ser validado antes de enviar, contradições com o que já foi dito ou decidido, questionamentos de processo de supply, riscos para a StartGi, o histórico relevante com as fontes citadas e o rascunho da resposta, com botões para ajustar o tom.

O coração do sistema é o **registro de decisões**: a regra que vale para cada assunto, com a fonte e quem aprovou do lado da Motiva. O funcional usa esse registro como referência principal, e cada análise sugere novas decisões, que só passam a valer depois de revisadas.

## Publicar no Render

1. Crie um repositório no GitHub e suba **todos os arquivos de uma vez, num único commit** (Add file → Upload files, arrastando a pasta inteira). Subir arquivo por arquivo gera deploys parciais.
2. No Render: New → Blueprint → escolha o repositório. O `render.yaml` cria o serviço web e o banco Postgres.
3. Preencha as variáveis secretas: `APP_PASSWORD`, `ZOHO_EMAIL`, `ZOHO_APP_PASSWORD` e `AI_API_KEY`.
4. Use o plano pago do serviço web (starter). No plano free o serviço dorme e a leitura do Zoho para.

## Configurar o Zoho Mail

No Zoho Mail, habilite o acesso IMAP (Configurações → Contas de e-mail → IMAP). Depois gere uma senha específica de aplicativo (Minha conta → Segurança) e use em `ZOHO_APP_PASSWORD`. O sistema abre as pastas em modo somente leitura.

Se a conta for de outro datacenter, ajuste `ZOHO_IMAP_HOST` (`imap.zoho.com`, `imap.zoho.eu` ou `imappro.zoho.com` para contas de organização). Confira os nomes das pastas em `ZOHO_FOLDERS` (padrão `INBOX,Sent`) e os domínios da Motiva em `MOTIVA_DOMAINS`. Só e-mails que envolvem esses domínios são guardados.

Na primeira sincronização, o sistema importa o histórico dos últimos `ZOHO_FIRST_SYNC_DAYS` dias (padrão 365) sem gerar briefings. A partir daí, cada e-mail novo da Motiva ganha briefing automático.

## IA

Por padrão usa a API da Anthropic (`AI_PROVIDER=anthropic`, `AI_MODEL=claude-sonnet-5`). Para outro provedor com API compatível com OpenAI (OpenRouter, servidor próprio etc.), use `AI_PROVIDER=openai`, `AI_BASE_URL` e o `AI_MODEL` correspondente.

## Primeiros passos depois de publicar

1. **Base de conhecimento:** suba as EFs, a documentação do Coupa de abril, a documentação do Wellington de 23/09 e as documentações do Lumos. A exportação do grupo de WhatsApp do time (grupo → Exportar conversa, sem mídia) entra como tipo "WhatsApp"; repita a exportação de tempos em tempos.
2. **Registro de decisões:** cadastre as regras já acordadas, cada uma com a fonte e quem aprovou. Comece pelos assuntos em disputa (tracking de etapas, reprovação de estratégia no Coupa, LOF). Sem esse registro, o funcional depende só dos e-mails, que têm versões contraditórias da mesma regra.
3. **Validador de regras:** cole na base os resultados do validador do Lumos que forem evidência de uma discussão (tipo "Resultado do validador de regras"). A integração direta com o validador fica para a fase 2.

## Segurança

O acesso à interface exige usuário e senha (`APP_USER` e `APP_PASSWORD`). Os e-mails contêm dados da Motiva e são enviados ao provedor de IA configurado; confira a cláusula de confidencialidade do contrato antes de ligar em produção.

## Rodar localmente

```
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Sem `DATABASE_URL`, usa SQLite local. Sem Zoho configurado, dá para testar colando e-mails manualmente na Caixa.
