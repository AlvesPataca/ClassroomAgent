# IMPLEMENTATION REPORT — FASE 2

## Resultado

Fase 2 implementada sobre a Fase 1 validada. A integração Google, OAuth, regras de domínio, timezone `America/Sao_Paulo` e CLI existente foram preservados. O novo fluxo é `python main.py sync`; depois dele, `courses`, `assignments` e `pending` leem o snapshot SQLite. `--api` força a consulta direta ao Classroom.

## Arquitetura e schema

- `app/persistence/models.py`: SQLAlchemy declarative models e `AwareTimestamp` (SQLite armazena UTC ISO e restaura datetimes aware).
- `app/persistence/database.py`: cria `data/classroom.db`, ativa foreign keys, `busy_timeout`, valida `PRAGMA user_version` e recusa schema incompatível.
- `app/persistence/repository.py`: repositório separado; IDs locais inteiros, Google IDs únicos, FKs e payloads sanitizados.
- `app/persistence/reader.py`: leitura coerente de snapshot local.
- `app/sync.py`: sync com `sync_runs`, transação por disciplina (`BEGIN IMMEDIATE`), rollback e PARTIAL.

Tabelas: `courses`, `assignments`, `submissions`, `sync_runs`. Cada registro guarda `raw_payload` relevante sem tokens, snapshot normalizado, fingerprint, `first_seen_at`, `last_seen_at`, `last_synced_at` e timestamps Google quando disponíveis.

## UPSERT e mudanças

As constraints únicas usam `google_id` (e o pai local em atividades/submissões). O fingerprint exclui somente timestamps Google e `last_synced_at`; portanto título, descrição, prazo, nota, estado e conteúdo/estado de submissão geram `UPDATED`, enquanto repetição sem mudança gera `UNCHANGED`. Registros que reaparecem reutilizam o mesmo ID local. Ausências são marcadas como `present=false`, preservando histórico.

## Validação

Executado na cópia final:

```text
ruff check .       -> All checks passed
mypy .             -> Success: no issues found in 26 source files
pytest -q          -> 99 passed in 3.24s
```

Os testes cobrem schema temporário, constraints, UPSERT repetido, detecção NEW/UPDATED/UNCHANGED, alteração de prazo/descrição/submissão/nota, timestamps isolados, rollback, SUCCESS/PARTIAL/FAILED, ordenação de `pending`, `MISSING`/`PENDING`, timezone e CLI sem credenciais. `compileall` e `python main.py --help` permanecem disponíveis.

## Teste com a conta real

Na pasta do projeto:

```powershell
python -m pip install -e ".[dev]"
python main.py auth
python main.py sync
python main.py courses
python main.py assignments
python main.py pending
python main.py pending --api
```

Confira `data/classroom.db` e o `Sync #...` exibido. Se uma disciplina falhar, o comando retorna código 1 e registra `PARTIAL`; o snapshot anterior dela permanece intacto. Nenhum segredo é impresso ou persistido no payload bruto.

## Limitações

SQLite não oferece timezone nativo, por isso a camada converte para UTC e reconstitui valores aware. Migrações futuras exigirão versão explícita; Alembic não foi adicionado nesta fase. A leitura local reflete o último sync e não busca alterações automaticamente. Drive, anexos, extração de conteúdo, IA/Astra, geração de respostas e entrega continuam fora do escopo.

Próxima fase: **Fase 3 — Google Drive, anexos e extração de conteúdo**.
