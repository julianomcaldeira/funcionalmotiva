# AGENTS.md

## Fluxo de trabalho obrigatório

Este projeto é o **FuncionalMotiva** e está conectado ao repositório GitHub:

- Repo: `https://github.com/julianomcaldeira/funcionalmotiva.git`
- Branch: `main` (tracking `origin/main`)

Após **qualquer** alteração no código, o fluxo obrigatório é:

1. `pip install -r requirements.txt` (instalar/atualizar dependências)
2. `python -m py_compile app/*.py` (verificar que compila sem erros)
3. `git add <arquivos alterados>`
4. `git commit -m "<mensagem descritiva da alteração>"`
5. `git push origin main`

O push para o GitHub é o que faz o **Render** refletir as mudanças no deploy. Nunca finalizar uma tarefa sem commit + push.

## Comandos

- Instalar deps: `pip install -r requirements.txt`
- Rodar dev: `uvicorn app.main:app --reload`
- Build: `pip install -r requirements.txt`
- Lint: `python -m py_compile app/*.py`
