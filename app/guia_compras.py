"""Primeira versão do Guia de Compras: entra em toda análise, chat e resposta do assistente.

O texto descreve a prática de mercado de compras em grandes empresas de infraestrutura como a Motiva.
Pontos marcados com [CONFIRMAR] ainda precisam ser validados com a Motiva e devem ser editados na
Base de conhecimento → Guia de Compras.
"""

GUIA_NOME = "Guia de Compras — cadeia de suprimentos da Motiva"

GUIA_PADRAO = """GUIA DE COMPRAS — COMO FUNCIONA A CADEIA DE SUPRIMENTOS DA MOTIVA
Versão inicial escrita a partir da prática de mercado de grandes empresas de infraestrutura e do que o projeto Lumos já mostra. Itens marcados [CONFIRMAR] ainda não foram validados com a Motiva: use como hipótese, nunca como regra acordada. Regra acordada mora no Registro de Decisões.

1. VISÃO GERAL
A Motiva (antigo Grupo CCR) opera concessões de rodovias, mobilidade urbana (metrô, trens, VLT, barcas) e aeroportos. Compra muito serviço de engenharia, obras e manutenção (CAPEX e OPEX), além de materiais e serviços gerais. Por ser companhia aberta e concessionária regulada, o processo de compras é formal, rastreável, com alçadas de aprovação, compliance e auditoria.
O suprimento costuma ser centralizado num Centro de Serviços Compartilhados (CSC/Supply) que atende as unidades de negócio (as concessionárias).
Papéis típicos:
- Requisitante: área da concessionária que precisa do material ou serviço.
- Aprovador da requisição: gestor do requisitante, conforme alçada de valor.
- Comprador: Supply/CSC, conduz o sourcing, a negociação e a contratação.
- Aprovadores da estratégia e da adjudicação: gestores de Supply e da área, conforme alçada.
- Jurídico: minuta e revisão do contrato.
- Fornecedor: participa da concorrência, assina o contrato, entrega e fatura.
- Contas a pagar / fiscal: recebimento da nota fiscal e pagamento.

2. A CADEIA, ETAPA POR ETAPA (source-to-pay)
2.1 Demanda e requisição de compra (RC)
- A necessidade nasce na área e vira uma requisição no SAP MM (ou no Coupa, conforme o fluxo). [CONFIRMAR onde a RC nasce na Motiva: SAP ou Coupa.]
- A RC traz item/serviço, quantidade, centro de custo ou ordem/PEP (CAPEX), data necessária e justificativa.
- Aprovação da RC por workflow, conforme alçada de valor e tipo de gasto.
- Se já existe contrato vigente ou lista de preços (LPU) para o item, a compra pula a concorrência e vira pedido direto contra o contrato. Senão, segue para sourcing.

2.2 Estratégia de sourcing
- O comprador define como vai comprar: concorrência (carta convite / RFQ / RFP), negociação direta, compra emergencial ou fornecedor único (com justificativa), uso de contrato existente.
- A estratégia é formalizada e aprovada antes de ir ao mercado (no Coupa: aprovação da estratégia de sourcing). Reprovação devolve ao comprador com motivo. No Lumos, o tracking precisa mostrar esse vai-e-volta e quem aprovou/reprovou.

2.3 Fornecedores: cadastro e homologação
- Só participa fornecedor cadastrado e homologado: dados cadastrais, fiscais, trabalhistas, financeiros e checagem de integridade (due diligence anticorrupção, Lei 12.846). Para serviços críticos, há qualificação técnica e de SSMA (segurança, saúde e meio ambiente).
- O cadastro vive no Coupa (portal do fornecedor) e é replicado ao SAP. Divergência de CNPJ entre os sistemas é causa comum de erro de integração.

2.4 Cotação / concorrência (RFx)
- RFI (informação), RFP (proposta técnica e comercial), RFQ (preço). Carta convite = convite formal a um grupo de fornecedores homologados, com escopo, prazo e critérios.
- Rodadas de proposta e de negociação (pode haver BAFO, a "melhor e final oferta").

2.5 Equalização e tabela comparativa
- Equalização técnica: confirma que todos cotaram o mesmo escopo; quem não atende é desclassificado.
- Equalização comercial: coloca propostas na mesma base (impostos, frete, condições de pagamento, prazo).
- A tabela comparativa (mapa de cotação) consolida tudo e é a peça central da decisão. Ela sustenta a escolha e é revisada pela auditoria.

2.6 Adjudicação e aprovação
- Escolha do vencedor com justificativa (menor preço global, técnica e preço, ou outro critério definido na estratégia) e aprovação por alçada. Saving = referência (orçamento/última compra/primeira proposta) menos valor negociado. [CONFIRMAR qual referência a Motiva usa para o saving.]

2.7 Contratação
- Serviços e obras geralmente geram contrato: minuta do jurídico (Netlex), negociação de cláusulas, assinatura eletrônica (DocuSign). Garantias, seguros e documentação do fornecedor são exigidos antes do início.
- No SAP o contrato vira um acordo-quadro (outline agreement): contrato por valor ou por quantidade, ou lista de preços unitários (LPU) para chamadas recorrentes. [CONFIRMAR o significado dos tipos de contrato "K", "F" e "LPU U" usados nos painéis da Motiva.]
- Aditivos alteram prazo, valor ou escopo e passam por nova aprovação.

2.8 Pedido de compra (PO)
- O pedido é emitido no SAP (com referência ao contrato, quando houver) e enviado ao fornecedor. É o documento que autoriza o fornecimento.

2.9 Recebimento e medição
- Material: entrada de mercadoria (MIGO) no almoxarifado ou direto no local.
- Serviço e obra: medição do que foi executado no período, aprovada pelo gestor do contrato (folha de registro de serviço no SAP). Sem medição aprovada não há pagamento.

2.10 Nota fiscal e pagamento
- O fornecedor emite a nota contra o pedido; o fiscal confere (três vias: pedido × recebimento/medição × nota) e lança (MIRO). Contas a pagar paga conforme a condição negociada. Divergência de valor ou quantidade bloqueia o pagamento.

2.11 Gestão do fornecedor
- Avaliação de desempenho (prazo, qualidade, SSMA), ocorrências, penalidades e renovação ou encerramento do contrato.

3. SISTEMAS E QUEM FAZ O QUÊ
- SAP (MM): requisição, pedido, contrato/acordo-quadro, recebimento, medição, nota e pagamento. Sistema de registro financeiro.
- Coupa: portal do fornecedor, cadastro, sourcing (eventos de cotação), aprovação de estratégia. [CONFIRMAR quais módulos do Coupa a Motiva usa.]
- Netlex: gestão de contratos e minutas do jurídico.
- DocuSign: assinatura eletrônica.
- Lumos (StartGi): recebe dados de SAP, Coupa, Netlex e DocuSign e mostra painéis, tracking de etapas, LOF e tabela comparativa. É RECEPTOR: não altera dado de origem. Se o dado está errado na origem, o Lumos mostra errado; a correção é na origem.

4. INDICADORES QUE A MOTIVA ACOMPANHA
- SLA de Supply: prazo-alvo por etapa ou do processo inteiro (da RC aprovada ao pedido ou contrato assinado). O SLA costuma variar por tipo de compra e complexidade, e a regra de "quando o relógio para" (pedido emitido, contrato aprovado, contrato assinado) precisa ser explícita. Ver decisões sobre ordem de prioridade do SLA.
- TMA (tempo médio de atendimento): média de dias entre início e fim de uma etapa, para o requisitante e para Supply. Devoluções e reprovações reiniciam ou pausam a contagem, conforme a regra acordada.
- Dentro × fora do prazo: total de processos = dentro do prazo + fora do prazo; se não fechar, há processo sem data de fim, sem SLA cadastrado ou com etapa ignorada.
- Backlog: processos abertos por etapa, por comprador e por idade.
- Saving, lead time por etapa, % de compras emergenciais, % de compras sem concorrência, aderência a contrato.

5. O QUE COSTUMA GERAR CONFLITO NUM PROJETO COMO O LUMOS
- Data de início e fim de cada etapa: qual evento do SAP/Coupa marca cada uma.
- Processos que voltam (reprovação, devolução ao requisitante, nova rodada): como contam no TMA e no SLA.
- Agrupamentos (POOL de grupos de compras, unidades, categorias): lista fixa ou configurável.
- Dados que não existem na origem (ex.: motivo de devolução): o Lumos não inventa; precisa de melhoria no sistema de origem.
- Diferença entre ambiente de validação e produção, e entre carga histórica e carga diária.
Ao responder a Motiva, deixe claro de qual sistema vem o dado, qual regra foi aplicada e em qual decisão registrada isso foi acordado.

6. GLOSSÁRIO
RC: requisição de compra. PO / pedido: pedido de compra. LPU: lista de preços unitários. RFx: RFI, RFP, RFQ. Carta convite: convite formal a fornecedores para cotar. BAFO: melhor e última oferta. Equalização: colocar propostas na mesma base. Tabela comparativa / mapa: comparação final das propostas. Adjudicação: escolha do vencedor. Alçada: limite de valor que cada aprovador pode aprovar. CAPEX: investimento. OPEX: custeio. PEP: elemento do projeto (CAPEX) no SAP. MIGO: entrada de material no SAP. MIRO: lançamento da nota no SAP. Folha de serviço / medição: registro do serviço executado. CSC: centro de serviços compartilhados. SSMA: segurança, saúde e meio ambiente. SLA: prazo-alvo. TMA: tempo médio de atendimento. LOF: [CONFIRMAR significado usado no Lumos]. GAV: [CONFIRMAR significado usado no painel do comprador].

7. PERGUNTAS PARA FECHAR COM A MOTIVA
- Onde nasce a RC e qual sistema é a fonte oficial de cada etapa?
- Quais são as alçadas de aprovação (RC, estratégia, adjudicação, contrato)?
- Qual o SLA por tipo de compra e qual evento encerra a contagem?
- Significado de K, F, LPU U, LOF e GAV nos painéis.
- Referência de cálculo do saving.
"""
