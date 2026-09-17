# Classroom Agent — Fases 1–4

CLI Python 3.11+ somente leitura: Classroom v1, SQLite/SQLAlchemy, Drive v3 e
extração local de anexos. A Fase 4 acrescenta Context Discovery, leitura oficial
somente-leitura de Forms quando possível, provider Mock/Astra e soluções estruturadas
versionadas para revisão humana. Não há turn-in, envio de Forms ou escrita no Google.

## Fase 4

```powershell
python main.py context <id>
python main.py context <id> --json
python main.py solve <id> --provider mock
python main.py solutions <id>
```

`description` é fonte primária: uma atividade description-only pode ficar `Ready for
AI: SIM` sem anexos. Forms detectado sem perguntas oficiais disponíveis fica
`FORM_DETECTED_BUT_QUESTIONS_UNAVAILABLE` e bloqueia `solve`, exceto quando a
description já é suficiente. O texto de professores, Forms, links e anexos entra
como `UNTRUSTED_DATA`; o provider só produz JSON validado e toda solução fica
`NEEDS_REVIEW`.

O provider padrão é `mock` e não chama rede. Para usar Astra localmente, configure
`ASTRA_API_KEY` e `LLM_PROVIDER=astra`; a implementação usa a Responses API oficial,
`gpt-6-astra`, `store=false`, sem tools, e exige saída JSON Schema. O acesso do
modelo no Work não autentica automaticamente este processo local.

Para testar uma atividade description-only real:

```powershell
python main.py sync
python main.py local-assignments
python main.py context ID
python main.py solve ID --provider mock
python main.py solutions ID
```

Nenhum desses comandos submete a atividade ou responde o Forms.

## Instalação

Na raiz do projeto:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Use `.venv\Scripts\python.exe` no lugar de `python` nos próximos comandos,
ou ative o ambiente com `.venv\Scripts\Activate.ps1`.
Linux/macOS: `source .venv/bin/activate` e `cp .env.example .env`.
Instalação sem ferramentas de desenvolvimento: `python -m pip install -r requirements.txt`.

## Migração e reautenticação

1. Encerre processos que usem o banco anterior. Faça backup do arquivo configurado
   em `DATABASE_FILE` na Fase 2. O padrão é `data/classroom.db`.
2. Para usar esta nova pasta, copie o banco anterior para `data/classroom.db` nela.
   Copie seu `credentials.json` OAuth Desktop. Não copie caches ou `.venv`.
   Se usar caminhos diferentes, ajuste `DATABASE_FILE` e `GOOGLE_CREDENTIALS_FILE`.
3. No [Google Cloud Console](https://console.cloud.google.com/), selecione o projeto
   do cliente OAuth. Habilite **Google Classroom API** e **Google Drive API**.
   Configure consentimento e Test users quando aplicável. Contas escolares podem
   precisar de autorização do administrador para o novo scope.
4. Se já existir token nesta pasta, renomeie o arquivo efetivamente configurado em
   `GOOGLE_TOKEN_FILE`. Para o padrão:

```powershell
if (Test-Path -LiteralPath token.json) {
    Rename-Item -LiteralPath token.json -NewName ("token-fase2-" + (Get-Date -Format yyyyMMddHHmmss) + ".json")
}
python main.py auth --debug
```

Mantenha `credentials.json`. Autorize a conta aluna que pode ler os anexos.
O navegador abre apenas em `auth`. Confirme **Conta autenticada. Acesso somente
leitura.** no terminal; a página de callback apenas confirma recebimento.
Tokens renomeados continuam sendo secrets e não devem ser compartilhados.
Renomear o token não revoga o consentimento no Google.

Scopes solicitados:

```text
https://www.googleapis.com/auth/classroom.courses.readonly
https://www.googleapis.com/auth/classroom.coursework.me.readonly
https://www.googleapis.com/auth/classroom.student-submissions.me.readonly
https://www.googleapis.com/auth/drive.readonly
```

Somente `drive.readonly` foi adicionado. Docs/Slides/Sheets são exportados pelo
Drive, sem scopes extras dessas APIs. `drive.file` não é readonly e não atende
à leitura geral dos anexos preexistentes. O aplicativo não enumera o Drive:
consulta somente IDs presentes nos materiais persistidos do CourseWork.

Tokens sem novos scopes são recusados antes de renovação/uso, com mensagem clara
para reautenticar, inclusive em `sync`. A validação usa `granted_scopes`, preserva
sua evidência no token e mantém a equivalência restrita dos aliases readonly do
próprio aluno. Não desabilita verificações OAuth. O token é salvo atomicamente.
Debug mostra scopes e booleanos, nunca valores de tokens/client secret.

Abertura do SQLite migra `user_version` 0/1 para 2 de forma aditiva, criando
`attachments`. As quatro tabelas anteriores, IDs e registros são preservados.
Schemas incompatíveis/versões futuras são recusados. A Fase 2 não armazenava
materiais: **execute sync novamente após migrar**, ou a tabela estará vazia.

## Testar com uma atividade real

Depois da instalação, cópia dos arquivos e reautenticação:

```powershell
python main.py sync
python main.py courses
python main.py assignments
python main.py local-assignments
```

Confirme `Sync #...: SUCCESS`. Em `local-assignments`, escolha uma atividade que
tenha documento acessível e anote seu **ID local inteiro**, não o ID Google.
Substitua `123` abaixo pelo ID mostrado:

```powershell
$atividade = 123
python main.py attachments $atividade
python main.py fetch-attachments $atividade
python main.py extract $atividade
python main.py attachments $atividade
```

Espere `DOWNLOADED` após fetch e `EXTRACTED` após extract para formatos suportados,
com caracteres/páginas e caminho. Links, YouTube, Forms e imagens podem mostrar
`UNSUPPORTED` com motivo. Repita os comandos: IDs e caminhos devem permanecer
estáveis, sem novo download se versão e arquivo local íntegro forem iguais.
Altere um documento Drive sob seu controle e repita fetch/extract: bytes diferentes
ganham novo hash/caminho; os anteriores permanecem. Para conferir remoção, retire
um material de uma atividade que você pode editar na interface Google e execute
sync: a listagem deve mostrar `REMOVED`, mantendo histórico.

Testes automatizados não comprovam acesso da conta ou políticas da escola.
`PARTIAL`/`FAILED` exige resolver a mensagem antes de assumir descoberta completa.

## CLI

| Comando | Comportamento |
|---|---|
| `auth [--debug]` | OAuth Desktop readonly |
| `sync` | Cursos, atividades, submissões e metadata dos materiais |
| `courses`, `assignments`, `pending` | Snapshot SQLite |
| Os mesmos com `--api` | Leitura Classroom direta, sem gravar banco |
| `assignments --course ID_GOOGLE` | Filtro por disciplina, preservado |
| `pending --include-archived` | Inclui disciplinas arquivadas |
| `local-assignments` | IDs locais para operar anexos |
| `attachments ID_LOCAL` | Metadata, URL sanitizada, Drive ID, presença/status e caminho |
| `fetch-attachments ID_LOCAL` | Consulta Drive e obtém/exporta arquivos autorizados |
| `extract ID_LOCAL` | Extração local; imprime somente resumo |

`attachments`, `extract` e `local-assignments` não autenticam nem usam rede.
Fetch respeita `capabilities.canDownload`, lixeira e versão antes/depois da
transferência. Extract exige fetch prévio e não usa bytes antigos após falha de
permissão/mudança detectada. Falhas por anexo permitem continuar os demais e
retornam código 1; `UNSUPPORTED` é resultado esperado. O snapshot é local:
revogações de acesso só são conhecidas quando Drive é consultado novamente.

## Persistência e estados

`attachments`: ID inteiro, FK `assignment_id`, identidade única por atividade,
tipo, título, Drive ID/URL, MIME original/materializado, caminho relativo,
SHA-256, fingerprint de metadata materializada, payload sanitizado, metadata
Drive permitida, status, erros seguros, texto estruturado, histórico JSON e
timestamps UTC de descoberta/atualização/download/extração/remoção.

Identidade usa tipo + Drive/video ID ou URL sanitizada. Material desconhecido
sem identidade usa posição na lista; nesse caso, reordenação pode parecer
alteração de slot. Repetições idênticas são deduplicadas. Mudança de material
marca a atividade UPDATED. Remoção marca `present=false` e `removed_at`, sem
apagar arquivos ou registros. Reaparecimento reutiliza o ID. Histórico preserva
metadata/caminho/hash anteriores; texto antigo pode ser reconstruído dos bytes.

Estados: DISCOVERED, DOWNLOADED, EXTRACTED, UNSUPPORTED, FAILED. Presença/remoção
é independente do processamento. Curso/atividade ausente bloqueia novas operações
e contexto. Descoberta compartilha a transação por disciplina; falha posterior
em submissões reverte também anexos. SUCCESS/PARTIAL/FAILED, UPSERT, leitura local
e --api permanecem. Fetch/extract serializam escritas com sync: outra operação
pode aguardar o lock do SQLite durante arquivos lentos.

## Formatos

| Origem | Obtenção | Extração |
|---|---|---|
| TXT/MD UTF-8 ou UTF-16 com BOM | files.get, alt=media | Texto literal |
| PDF | files.get, alt=media | pypdf, texto por página |
| DOCX | files.get, alt=media | XML seguro; parágrafos e texto de tabelas |
| Google Docs | files.export, text/plain | TXT |
| Google Slides / PPTX | Export PPTX / download | Texto por slide |
| Google Sheets / XLSX | Export XLSX / download | Células por planilha/linha e valores em cache |
| LINK/YOUTUBE/FORM/UNKNOWN | Somente metadata | Sem scraping/browser |
| Imagens/outros binários | Somente metadata | UNSUPPORTED; imagens IMAGE_NO_OCR |

PDF sem texto: NO_TEXT_LAYER. PDF protegido: ENCRYPTED. Sem OCR, execução de
scripts/macros/JavaScript, avaliação de fórmulas, Office automation ou chamadas
a links externos dos documentos. Atalhos/pastas Drive não são seguidos.
DOCX não extrai cabeçalhos/rodapés/notas; Slides não inclui notas do apresentador;
Sheets usa planilhas numeradas, coordenadas e resultados em cache, sem converter
estilos/datas nem recalcular fórmulas. Exportar Google Docs em TXT perde layout.

Requisições de download são construídas pelo discovery oficial Drive v3;
transporte OAuth usa streaming limitado. URLs dos materiais/exportLinks nunca
são destinos de download. Redirecionamentos são recusados. Exportação Drive tem
limite próprio de 10 MB, mesmo quando o limite local for maior.

## Segurança e configuração

Pasta fixa da CLI: `data/attachments/<course-local-id>/<assignment-local-id>/`.
Nomes contêm ID local, SHA-256 completo e nome sanitizado. Conteúdo igual reutiliza
arquivo; conteúdo diferente ganha outro nome. Criação exclusiva não sobrescreve
arquivo existente. Hash detecta adulteração: mova o arquivo adulterado para fora
da pasta controlada antes de repetir fetch.

- Allowlist de formatos; bloqueio de executáveis/scripts/macros, extensão dupla
  e assinaturas executáveis. Texto extraído nunca é executado.
- Proteção contra traversal, nomes reservados Windows, caminhos externos,
  symlinks/junctions/hardlinks. Leitura exige nome gerado, IDs e hash correspondentes.
- Extração recebe bytes validados, não caminhos arbitrários ou comandos.
  `.env`, `credentials.json`, `token.json` não são anexos válidos. OAuth lê seus
  arquivos configurados apenas para autenticação; não os inclui no contexto.
- XML com defusedxml, sem entidades externas. ZIP não é extraído no filesystem:
  verifica expansão, entradas, traversal, macros e objetos incorporados.
- Parser em processo separado com timeout; não é sandbox de sistema operacional.
  Limites de bytes não equivalem a teto exato de RAM. Mantenha bibliotecas atualizadas.
- Resumos removem controles/markup; erros não expõem conteúdo bruto de API/parser.
  URLs removem credenciais, fragmentos e parâmetros exceto id/v; isso pode
  simplificar links cuja navegação dependa de outros parâmetros.
- SQLite/anexos locais não são criptografados; no Windows proteção depende das
  ACLs do usuário. Nenhum conteúdo é enviado a LLM ou serviço de IA.

| Variável | Padrão |
|---|---|
| DATABASE_FILE | data/classroom.db |
| GOOGLE_CREDENTIALS_FILE / GOOGLE_TOKEN_FILE | credentials.json / token.json |
| TIMEZONE | America/Sao_Paulo |
| HTTP_TIMEOUT_SECONDS | 30 (1–300) |
| MAX_RETRIES | 3 (0–8) |
| ATTACHMENT_MAX_BYTES | 20971520, até 100 MiB |
| EXTRACTION_MAX_BYTES | 41943040, até 100 MiB; ZIP expandido/stream PDF |
| EXTRACTION_MAX_CHARS | 1000000, até 5000000; documento e anexos do bundle |
| EXTRACTION_TIMEOUT_SECONDS | 30 (1–120) |

Também há limites de 5000 entradas ZIP e 2000 páginas PDF. Variáveis do processo
prevalecem sobre `.env`; caminhos relativos resolvem contra a raiz do projeto.
Retry com backoff/jitter só em 429, 500/502/503/504, falha de transporte e 403
explicitamente classificado como rate limit. 401, 403 de acesso, 404, quota diária,
scope insuficiente, API desativada e formato inválido não são repetidos.

## Contexto para a próxima fase

`app.attachments.context.build_context(engine, assignment_local_id, settings)`
retorna ContextBundle com disciplina, título/enunciado, prazo, submissões e anexos
presentes com seções extraídas. `to_dict()` serializa. Texto é encapsulado em
`UntrustedText(trust="UNTRUSTED_DATA")`; ExtractedContent também carrega essa marca,
schema, caracteres e páginas. Sem slots administrativos, prompts ou dispatch por
texto. Payloads brutos, URLs, caminhos e credenciais são excluídos por allowlist.
Isso preserva a fronteira de dados, sem prometer imunidade de um futuro LLM a
prompt injection. Nenhum LLM foi conectado nesta fase.

## Regras Classroom preservadas

NEW/CREATED/RECLAIMED_BY_STUDENT são PENDING, ou MISSING após o prazo.
TURNED_IN permanece entregue; RETURNED com assignedGrade (inclusive zero) é GRADED,
sem nota é RETURNED. Estado desconhecido/submissão ausente é UNKNOWN. Nota de
rascunho não significa correção publicada. `late` original é separado do atraso
calculado. Prazo é UTC na API, armazenado aware e exibido no timezone configurado.
`dueTime={}` é meia-noite; só um dos campos de prazo ausente é inválido.

## Verificação sem conta Google

```powershell
python -m ruff check .
python -m ruff format --check .
python -m mypy .
python -m pytest -q
python -m compileall -q app main.py
python main.py --help
```

Mocks ficam somente em tests/. Cobertura inclui regressões anteriores, migração
v1, descoberta/rollback, identidade, remoção, filesystem/limites, discovery oficial,
erros HTTP/retries, formatos, documentos corrompidos, cache, alteração durante
download, revogação, contexto e CLI sem documentos no terminal.

Referências oficiais:
[materiais Classroom](https://developers.google.com/workspace/classroom/reference/rest/v1/Material),
[download/exportação](https://developers.google.com/workspace/drive/api/guides/manage-downloads),
[formatos de exportação](https://developers.google.com/workspace/drive/api/guides/ref-export-formats),
[scopes Drive](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).

Próxima fase: Fase 4 — Context Builder + Astra/LLM + resolução estruturada
