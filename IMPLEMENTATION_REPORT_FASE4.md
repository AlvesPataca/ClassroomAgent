# IMPLEMENTATION REPORT — FASE 4

Implementada em `outputs/classroom-agent-fase4`, preservando os comandos das Fases
1–3 (`auth`, `sync`, `courses`, `assignments`, `pending`, `attachments`,
`fetch-attachments`, `extract`).

## Arquitetura

- `app/context/models.py`: modelos Pydantic canônicos, estritos e sem campos extras.
- `app/context/builder.py`: Context Discovery/Builder, classificação de origem e tipo,
  limites determinísticos, provenance, hash e `ready_for_ai`.
- `app/context/forms.py`: somente `GET /v1/forms/{formId}` por API oficial, usando o
  scope `drive.readonly` já existente; nunca abre navegador, raspa página ou envia resposta.
- `app/llm/providers.py`: `LLMProvider`, `MockProvider` e `AstraProvider` via Responses API
  documentada (`gpt-6-astra`). Astra exige configuração local explícita; não há acesso mágico
  ao modelo Work.
- `app/llm/schema.py`, `prompt.py`, `service.py`: `Solution` estrita, boundary
  `UNTRUSTED_ASSIGNMENT_DATA`, uma tentativa de reparo controlado e rejeição segura.
- `app/persistence/models.py`: `solutions`, com versão, provider/model, estado, hash,
  metadata sanitizada, resposta e timestamps. `user_version` passa a 3.

## Comportamento

Description-only suficiente fica utilizável sem anexos. Forms sem perguntas fica claramente
indisponível e bloqueia `solve` se não houver outra fonte suficiente. Combinações de description,
Forms e anexos preservam a provenance. Conteúdo truncado bloqueia solução para evitar resposta
baseada em contexto parcial. Hash diferente marca versões anteriores como stale.

Não foram implementados submit/turn-in, preenchimento/envio de Forms, browser automation,
execução de código baixado, geração DOCX/PDF ou aprovação automática.

## Verificação

```text
ruff check .       PASS
mypy .             PASS
pytest -q          272 passed
```

Os testes cobrem description-only, ausência de anexos, Forms disponível/indisponível/parcial,
combinações de fontes, provenance, truncamento, secrets/path, prompt injection, UNKNOWN,
MockProvider, respostas inválidas, versionamento, stale hash, concorrência, bloqueio de solve,
transporte Astra sem chamada real e regressão integral das Fases 1–3.
