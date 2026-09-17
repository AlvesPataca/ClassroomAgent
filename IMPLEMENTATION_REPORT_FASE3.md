# IMPLEMENTATION REPORT — FASE 3

Data: 17/09/2026. Versão: 0.3.0. Implementação concluída somente até a Fase 3.

## Resultado e preservação da base

A entrega parte do código real de `classroom-agent-fase2`, encontrado na pasta
outputs da tarefa anterior. A entrega anterior foi mantida intacta. Foi criada
uma nova pasta `classroom-agent-fase3` com Drive readonly, descoberta/persistência
de materiais, download/exportação controlados, extração e representação de contexto.
Não foram implementados Astra/LLM, prompts, resolução, respostas ou submissão.

A base original foi executada separadamente: **99 testes passaram**. Na nova base:
**227 testes passaram**, incluindo os 99 casos anteriores e 128 casos novos.
Nos testes anteriores, apenas expectativas de versão do schema e fixtures dos
aliases OAuth foram atualizadas para incluir o scope Drive obrigatório.

Não havia banco pessoal/credenciais na entrega anterior usada como fonte. Nenhum
banco pessoal foi migrado e nenhuma conta Google foi acessada durante a implementação.
A migração foi comprovada com banco v1 de teste contendo registros nas quatro tabelas.

## Arquivos

Novos:

- `app/attachments/materials.py`: parsing DRIVE_FILE/LINK/YOUTUBE/FORM/UNKNOWN,
  identidade, payload sanitizado, descoberta, alterações e remoção lógica.
- `app/attachments/drive.py`: Drive v3 oficial, metadata allowlist, streaming,
  download/export, permissões, checksum e retry transitório.
- `app/attachments/safety.py`: formatos permitidos, nomes/caminhos, bloqueios,
  armazenamento por hash e leitura controlada.
- `app/attachments/extraction.py`: ContentExtractor, ExtractedContent, seções,
  TXT/MD/PDF/DOCX/PPTX/XLSX e parser isolado com timeout.
- `app/attachments/service.py`: orquestração fetch/extract, estados, cache,
  revalidação, histórico e processamento individual dos anexos.
- `app/attachments/context.py`: ContextBundle e UntrustedText, sem LLM.
- `app/attachments/__init__.py` e `tests/test_attachments.py`.

Alterados:

- `app/config.py`, `app/auth/google.py`, `app/errors.py`: scope, limites e
  mensagem tipada de reautenticação, mantendo a validação OAuth anterior.
- `app/persistence/models.py`, `database.py`, `repository.py`: tabela nova,
  migração aditiva e descoberta na transação da atividade.
- `app/sync.py`: mantém erro de novos scopes visível em FAILED/PARTIAL.
- `app/cli/commands.py`: comandos de anexos e listagem de IDs locais.
- `pyproject.toml`, `.env.example`, `README.md` e duas expectativas de testes anteriores.

Clientes/mappers Classroom, modelos de domínio, regras de status/prazo e leitor
local não foram alterados. Relatórios antigos copiados continuam sendo registros
históricos das fases anteriores; este relatório e README descrevem a versão atual.

## Tabela e migração

Schema SQLite: `PRAGMA user_version=2`. Abertura migra 0/1 para 2 por criação
aditiva de `attachments`; não remove/reescreve tabelas ou IDs anteriores.
Versões futuras e schemas incompatíveis são recusados. A abertura repetida é idempotente.

`attachments` inclui ID local inteiro, assignment_id FK, identity_key único por
atividade, kind normalizado, title, drive_id, url, mime_type, materialized_mime,
local_path relativo, SHA-256, fingerprint de descoberta e de metadata materializada,
raw_payload sanitizado, metadata Drive permitida, status/erro seguro, extração JSON,
history JSON, presença e timestamps UTC. Constraints restringem tipos/estados.

Descoberta deduplica materiais, reconhece NEW/UPDATED/UNCHANGED na atividade,
marca remoção sem delete e reutiliza ID no reaparecimento. Mudanças arquivam
metadata/caminho/hash; arquivos antigos permanecem. Material desconhecido sem
identificador usa posição de lista, com a limitação de reordenação documentada.

A descoberta ocorre no mesmo rollback por disciplina das Fases 1/2. Não faz
chamadas Drive nem download durante sync. Como a Fase 2 não guardava materiais,
é necessário executar sync após migrar para popular attachments.

## OAuth, APIs e formatos

Scope único acrescentado:
`https://www.googleapis.com/auth/drive.readonly`.
Os três scopes Classroom readonly anteriores continuam solicitados. Os aliases
readonly do próprio aluno continuam equivalentes; não se aceitam permissões de
escrita/outros alunos como substitutos. Tokens sem Drive são recusados com instrução
de renomear GOOGLE_TOKEN_FILE e executar auth. Tokens/client secret não são exibidos.

Docs, Slides e Sheets usam exportação Drive; não exigem scopes adicionais dessas
APIs neste desenho. Operações são `files.get` de metadata, `files.get` com alt=media
e `files.export`, com URIs construídas pelo discovery oficial. Não há endpoint
inventado, enumeração geral do Drive, scraping, browser automation ou uso de
URLs externas como destinos de download.

| Arquivo | Obtenção | Extração |
|---|---|---|
| TXT, MD | Download binário | UTF-8/UTF-8 BOM e UTF-16 BOM |
| PDF | Download binário | Texto por página com pypdf; sem OCR |
| DOCX | Download binário | XML seguro, parágrafos e texto de tabelas |
| Google Docs | Export text/plain | TXT |
| PPTX / Google Slides | Download / export PPTX | Texto por slide |
| XLSX / Google Sheets | Download / export XLSX | Coordenadas, linhas, strings e valores em cache |
| LINK, YOUTUBE, FORM, UNKNOWN | Metadata apenas | Sem extração |
| Imagens/outros binários | Metadata apenas | UNSUPPORTED |

Download verifica autorização/canDownload, lixeira, tamanho, metadata antes/depois
e MD5 quando fornecido para binários. Versão estável mais arquivo local íntegro
permite pular transferência. Mudança detectada invalida extração. Falha ao obter
uma nova versão nunca promove bytes antigos como atuais. Bytes diferentes ganham
outro SHA-256/nome; não há sobrescrita silenciosa.

Limitações explícitas: export Drive tem teto próprio de 10 MB; TXT perde layout;
DOCX não inclui cabeçalhos/rodapés/notas; PPTX não inclui notas do apresentador;
XLSX usa planilhas numeradas e valores em cache, sem estilos/datas formatadas nem
recalcular fórmulas. Atalhos/pastas Drive não são seguidos. PDF sem camada textual
é NO_TEXT_LAYER; PDF criptografado é ENCRYPTED.

## CLI e contexto

Comandos implementados:

```text
python main.py local-assignments
python main.py attachments <assignment_local_id>
python main.py fetch-attachments <assignment_local_id>
python main.py extract <assignment_local_id>
```

auth/sync/courses/assignments/pending, filtros e --api permanecem. IDs dos comandos
anteriores continuam Google; os novos comandos de anexos recebem IDs locais.
Listagem e extração são locais, sem autenticação/rede. Resumos mostram nome,
tipo/MIME, status, caracteres/páginas e caminho, sem imprimir o documento inteiro.
Links/Drive IDs sanitizados aparecem na listagem de metadata. Anexos com falha não
interrompem os demais; falhas operacionais retornam código 1.

`build_context(engine, assignment_id, settings)` retorna disciplina, enunciado,
prazo, submissões e anexos presentes com texto extraído. Dados textuais são
`UntrustedText` marcados UNTRUSTED_DATA; ExtractedContent também carrega essa marca.
DTOs não contêm instruções administrativas, prompts, provider ou dispatch por texto.
Allowlist exclui raw payloads, URLs, caminhos e credenciais. Caminho/hash são
validados antes de disponibilizar texto; falhas de acesso detectadas bloqueiam
extração antiga. Um futuro LLM ainda precisará manter essa fronteira: a marca não
é uma promessa de imunidade a prompt injection.

## Segurança e estados

Pasta controlada: `data/attachments/<course-local-id>/<assignment-local-id>/`.
Criação exclusiva com nome sanitizado, ID e SHA-256. Proteções contra traversal,
caminhos absolutos/externos, nomes Windows reservados, colisões, symlinks, junctions
e hardlinks. Leitura exige estrutura gerenciada e hash correto. Arquivos sensíveis
.env/credentials.json/token.json não são anexos válidos. OAuth continua usando
seus arquivos somente para autenticar; o contexto não os lê nem envia.

Allowlist de formatos, bloqueio de extensões executáveis/scripts/macros (inclusive
duplas), assinaturas de executáveis, ZIP traversal/expansão excessiva/macros/objetos
incorporados e XML com entidades externas. Nenhum anexo, macro, fórmula, script ou
código baixado é executado. O processo filho executa apenas o parser confiável.
Não é sandbox de SO e os limites de bytes não são teto exato de RAM.

Configuração nova: ATTACHMENT_MAX_BYTES=20971520,
EXTRACTION_MAX_BYTES=41943040, EXTRACTION_MAX_CHARS=1000000,
EXTRACTION_TIMEOUT_SECONDS=30. Adicionalmente: 5000 entradas ZIP e 2000 páginas PDF.

Estados: DISCOVERED/DOWNLOADED/EXTRACTED/UNSUPPORTED/FAILED; presença/remoção
separada. Tratamento seguro de 401/403/404, Drive API desativada, scope insuficiente,
quota/rate limit, timeout, tamanho, MIME, PDF sem texto e corrupção. Retry com
backoff/jitter apenas transitório (inclui 403 de rate limit explicitamente identificado).
Texto bruto de erros das bibliotecas/provedor não vai para banco/terminal.

## Verificação executada

Ambiente: Windows, Python 3.14.7; pytest 9.1.1, ruff 0.16.8, mypy 1.20.2,
SQLAlchemy 2.0.54, pypdf 6.19.0, defusedxml 0.7.1.

| Verificação | Resultado |
|---|---|
| Base original Fase 2, pytest -q | 99 passed |
| Nova base, pytest -q | 227 passed, sem skips |
| ruff check . | All checks passed |
| ruff format --check . | Aprovado |
| mypy . | Success: no issues found in 34 source files |
| compileall app main.py | Aprovado |
| Imports de todos os módulos app | 25 módulos importados |
| Smoke tests CLI em processos reais | 14 aprovados, sem conta/rede Google |
| pip check | No broken requirements found |
| Build/inspeção wheel 0.3.0 | Aprovado; módulos/dependências de anexos presentes |

Cobertura nova: cada material, idempotência, mudança/remoção, rollback, migração
v1 populada, nomes/traversal/colisões, executáveis disfarçados, hardlinks/links,
limites de stream e ZIP, decisão download/export via discovery oficial,
TXT/MD/PDF-text/DOCX/PPTX/XLSX, PDF vazio/corrupção, entidades XML/macros,
scopes, 403/404, API desativada/quota, retry transitório/reinício de stream,
timeout, checksum, cache/revisões/revogação, contexto sem paths/secrets e CLI.
Mocks existem somente em testes. Não foi feito teste autenticado com conta real.

## Passos exatos para testar uma atividade real

1. Abra PowerShell na pasta entregue `classroom-agent-fase3`.
2. Se pretende preservar um banco real da Fase 2, encerre quem o usa, faça backup
   e copie o arquivo apontado por DATABASE_FILE naquela instalação para
   `data/classroom.db` desta pasta. Se preferir manter outro caminho, coloque o
   caminho absoluto em DATABASE_FILE no .env. Não use bancos de fixtures dos testes.
3. Execute:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Se o .env já existir com sua configuração, preserve-o e acrescente as variáveis
da .env.example, em vez de sobrescrevê-lo. Ajuste DATABASE_FILE antes do primeiro sync.

4. Coloque seu JSON OAuth Desktop em `credentials.json` nesta pasta. No projeto
   desse cliente no Google Cloud Console, habilite Classroom API e Drive API,
   e autorize sua conta como Test user se aplicável.
5. Para o token no caminho padrão, execute:

```powershell
if (Test-Path -LiteralPath token.json) {
    Rename-Item -LiteralPath token.json -NewName ("token-fase2-" + (Get-Date -Format yyyyMMddHHmmss) + ".json")
}
.venv\Scripts\python.exe main.py auth --debug
.venv\Scripts\python.exe main.py sync
.venv\Scripts\python.exe main.py local-assignments
```

Se GOOGLE_TOKEN_FILE foi personalizado, renomeie esse arquivo, não outro.
No navegador, autorize a conta que participa do curso e tem acesso aos documentos.
Confirme autenticação e SUCCESS no terminal.

6. Escolha na listagem um ID local de atividade com material acessível. Troque 123:

```powershell
$atividade = 123
.venv\Scripts\python.exe main.py attachments $atividade
.venv\Scripts\python.exe main.py fetch-attachments $atividade
.venv\Scripts\python.exe main.py extract $atividade
.venv\Scripts\python.exe main.py attachments $atividade
```

7. Confira DOWNLOADED → EXTRACTED, caracteres/páginas e caminho, ou o motivo
   UNSUPPORTED/FAILED. Repita fetch/extract: mesmos bytes devem manter caminho/hash
   e não duplicar registros. Em documento sob seu controle, altere o conteúdo e
   repita para verificar nova versão sem apagar a antiga.
8. Para verificar regressões localmente:

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy .
.venv\Scripts\python.exe -m pytest -q
```

Documentação completa: README.md e .env.example.
Referências oficiais usadas: [Material Classroom](https://developers.google.com/workspace/classroom/reference/rest/v1/Material),
[Drive downloads](https://developers.google.com/workspace/drive/api/guides/manage-downloads),
[export formats](https://developers.google.com/workspace/drive/api/guides/ref-export-formats),
[Drive scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).

Próxima fase: Fase 4 — Context Builder + Astra/LLM + resolução estruturada
