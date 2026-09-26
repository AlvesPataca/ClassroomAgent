# Classroom Agent

Agente local em Python para consultar o Google Classroom como aluno, sincronizar dados para SQLite, descobrir contexto de atividades, preparar soluções estruturadas com LLM, gerar PDFs revisáveis e os arquivos pedidos pela tarefa, e enviá-los para uma pasta controlada do Google Drive.

## Visão geral e fluxo

`Google Classroom → sync/persistência SQLite → contexto → solução → PDF + arquivos solicitados → revisão local → (upload explícito) Google Drive`

O Classroom fornece cursos, atividades, submissões e referências a materiais. `sync` mantém um snapshot local e também coleta materiais, comunicados e tópicos da turma. A descoberta de contexto reúne a descrição, associa materiais por tópico, texto e proximidade de publicação, além de links, Forms e anexos extraídos com provenance, limites e sanitização. `solve` grava uma resposta estruturada versionada; `generate` cria o documento de revisão em PDF, DOCX, TXT ou Markdown e materializa arquivos textuais pedidos, como HTML, CSS, JavaScript, Python, SQL, Markdown e CSV; `upload` envia os artefatos pendentes para a pasta de respostas configurada.

## Funcionalidades por fase

- **Fase 1:** cliente Classroom somente leitura, autenticação OAuth Desktop, listagem de cursos e atividades, status de submissões, tratamento de prazos e configuração por `.env`.
- **Fase 2:** sincronização incremental e idempotente em SQLite, preservação de snapshots e payloads, tombstones para registros removidos, anexos persistidos e leitura local.
- **Fase 3:** descoberta, download seguro e extração local de anexos suportados, com limites de tamanho, hash, estados, histórico e proteção contra conteúdo não confiável.
- **Fase 4:** descoberta de contexto canônico para atividades, leitura opcional de Google Forms, provenance, orçamento de contexto, soluções estruturadas validadas por schema, providers `mock`, Gemini e Astra/OpenAI-compatible, versionamento e comparação por hash.
- **Fase 5:** `DocumentBuilder` com PDFs versionados, templates `QUESTION_ANSWER`, `ACADEMIC_REPORT` e `CODE_ASSIGNMENT`, persistência de artifacts, organização no Drive por disciplina e upload explícito com scope mínimo `drive.file`.
- **Fase 6:** fluxo contínuo opcional, com sincronização imediata e verificações a cada 8 horas, seleção de atividades na segunda metade do prazo, solução e geração idempotentes, anexos no rascunho da submissão e sem `turnIn` automático.
- **Fase 6.1:** API privada versionada para saúde, estado, histórico, atividades recentes e solicitação segura de um ciclo manual.

## Arquitetura

`app/classroom` acessa a API do Classroom; `app/sync.py` coordena a sincronização; `app/automation.py` executa o fluxo contínuo; `app/persistence` define banco, modelos e leitor local; `app/attachments` descobre, baixa e extrai materiais; `app/context` monta o contexto seguro; `app/llm` chama o provider e valida a solução; `app/documents` renderiza e envia artifacts; `app/cli` expõe a interface Typer.

## Stack

Python 3.11+, Typer, Rich, SQLAlchemy 2, SQLite, Pydantic 2, Google API Client/Auth, `python-dotenv`, `pypdf`, ReportLab, Requests. Ferramentas de desenvolvimento: Ruff, mypy e pytest.

## Requisitos

- Windows com PowerShell e Python 3.11 ou superior.
- Projeto Google Cloud com APIs Google Classroom, Google Drive e, se Forms for usado, Google Forms habilitadas.
- Credencial OAuth do tipo **Desktop app** salva como `credentials.json` (ou no caminho definido por `GOOGLE_CREDENTIALS_FILE`).
- Conta com acesso às disciplinas e aos arquivos que serão consultados.
- Para upload, a conta precisa conceder o scope `drive.file`.

## Instalação no Windows/PowerShell

```powershell
git clone <URL_DO_REPOSITORIO>
cd classroom-agent-fase4
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Se a política do PowerShell impedir a ativação, use a sessão atual com `Set-ExecutionPolicy -Scope Process Bypass` ou invoque diretamente `.\.venv\Scripts\python.exe`.

### Instalação Python

Em qualquer ambiente Python 3.11+, crie um ambiente virtual e instale:

```bash
python -m venv .venv
python -m pip install -e ".[dev]"
```

`requirements.txt` contém as dependências de runtime; o `pyproject.toml` é a fonte da instalação do pacote e do extra de desenvolvimento.

## Configuração

Copie `.env.example` para `.env`. As variáveis principais são:

| Variável | Uso |
| --- | --- |
| `DATABASE_FILE` | SQLite local, por padrão `data/classroom.db`. |
| `GOOGLE_CREDENTIALS_FILE` | JSON OAuth Desktop baixado do Google Cloud. |
| `GOOGLE_TOKEN_FILE` | Token OAuth local, por padrão `token.json`. |
| `TIMEZONE` | Fuso usado para exibir prazos. |
| `GENERATED_ROOT` | Raiz dos PDFs gerados. |
| `LLM_PROVIDER` | Provider configurado; `mock` é seguro para testes locais. |
| `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL` | Credencial, modelo principal e fallback Gemini para erros 503 persistentes. |
| `ASTRA_API_KEY`, `ASTRA_BASE_URL`, `ASTRA_MODEL` | Provider compatível com API OpenAI, quando usado. |
| `CONTEXT_MAX_CHARS` | Limite do contexto montado. |
| `DRIVE_RESPONSES_FOLDER_ID` | ID da pasta raiz controlada para respostas; vazio cria/encontra `DRIVE_RESPONSES_FOLDER_NAME`. |
| `DRIVE_RESPONSES_FOLDER_NAME` | Nome usado quando o ID não é informado. |
| `DRIVE_ORGANIZE_BY_COURSE` | Quando `true`, cria uma subpasta por disciplina. |
| `AUTOMATION_INTERVAL_HOURS` | Intervalo entre ciclos contínuos; padrão `8` horas. |
| `LOG_LEVEL` | Nível do log (`DEBUG`, `INFO`, `WARNING`, `ERROR`); padrão `INFO`. |
| `API_HOST` | Bind da API privada; padrão seguro `127.0.0.1`. |
| `API_PORT` | Porta local da API; padrão `8765`. |
| `CYCLE_LOCK_FILE` | Lock compartilhado pelo agente e API; padrão `data/automation-cycle.lock`. |

Não coloque secrets no `.env.example`; use valores locais somente no `.env`.

## Google Cloud, OAuth e scopes

Habilite as APIs necessárias e crie credenciais OAuth Desktop. O fluxo de leitura solicita apenas scopes somente leitura:

- `classroom.courses.readonly`;
- `classroom.coursework.me.readonly`;
- `classroom.student-submissions.me.readonly`;
- `classroom.courseworkmaterials.readonly`;
- `classroom.announcements.readonly`;
- `classroom.topics.readonly`;
- `drive.readonly`.

Para geração e upload manual, `auth --write` solicita `drive.file` e `classroom.coursework.me`. O segundo permite anexar arquivos à própria submissão do aluno; ele não permite executar `turnIn`. Execute `python main.py auth --write` depois desta alteração. Se os scopes mudarem, renomeie ou remova apenas o arquivo indicado por `GOOGLE_TOKEN_FILE` e reautentique; não remova `credentials.json`.

## Autenticação

```powershell
python main.py auth
python main.py auth --write
```

O primeiro comando abre o navegador para os scopes de leitura. O segundo inclui `drive.file`. Comandos de leitura reutilizam e renovam o token local; nenhum token é impresso pelo CLI.

## CLI

Consulte sempre a ajuda instalada com `python main.py --help`. Os comandos atualmente disponíveis são:

```text
run [--once]
auth [--write]
sync
courses [--include-archived] [--api]
assignments [--course ID] [--include-archived] [--include-overdue] [--api]
pending [--course ID] [--include-archived] [--api]
local-assignments
attachments ASSIGNMENT_LOCAL_ID
fetch-attachments ASSIGNMENT_LOCAL_ID
extract ASSIGNMENT_LOCAL_ID
context ASSIGNMENT_LOCAL_ID [--offline] [--json]
solve ASSIGNMENT_LOCAL_ID [--provider PROVIDER] [--offline]
solutions ASSIGNMENT_LOCAL_ID [--offline]
solution SOLUTION_ID
generate ASSIGNMENT_LOCAL_ID [--template TEMPLATE] [--format pdf|docx|txt|md] [--solution-version N] [--upload]
artifacts ASSIGNMENT_LOCAL_ID
upload ASSIGNMENT_LOCAL_ID
```

`--api` consulta o Classroom diretamente; sem ele, os comandos de consulta usam o snapshot local. IDs de anexos e atividades usados por `attachments`, `context`, `solve`, `generate` e `upload` são IDs inteiros locais, exibidos por `local-assignments`.

`pending` mostra somente submissões ainda abertas e com prazo futuro. Atividades sem prazo também aparecem identificadas como `Sem prazo`; atividades vencidas ou já entregues ficam fora dessa listagem. `assignments` oculta prazos vencidos por padrão; use `--include-overdue` para diagnóstico histórico. `local-assignments` continua mostrando todos os registros persistidos, inclusive os marcados como removidos, sem apagar histórico.

### Fluxo completo

```powershell
python main.py run --once
python main.py auth
python main.py sync
python main.py local-assignments
python main.py attachments 12
python main.py fetch-attachments 12
python main.py extract 12
python main.py context 12 --json
python main.py solve 12 --provider mock
python main.py solutions 12
python main.py generate 12 --template academic-report
python main.py generate 12 --template academic-report --format docx
python main.py generate 12 --format txt
python main.py generate 12 --format md
python main.py artifacts 12
python main.py auth --write
python main.py upload 12
```

Sem subcomando, `python main.py` inicia o fluxo contínuo. Ele sincroniza a conta
imediatamente e repete a verificação a cada 8 horas (`AUTOMATION_INTERVAL_HOURS`).
Atividades publicadas, pendentes, com prazo definido e já na metade do intervalo entre
publicação e vencimento são preparadas automaticamente: anexos são baixados e extraídos,
o contexto é enviado ao provider configurado, a solução é salva para revisão, o PDF e
os arquivos pedidos são gerados, enviados ao Drive e anexados ao rascunho da submissão.
O fluxo nunca chama `turnIn`; a revisão e o envio final permanecem manuais. Use `run --once`
para testar uma rodada e `Ctrl+C` para encerrar o processo.

Substitua `12` pelo ID local mostrado no seu banco. O provider `mock` não chama serviço externo e é útil para verificar o fluxo.

### Fluxo contínuo

```powershell
# Uma rodada imediata, sem esperar o intervalo
python main.py run --once

# Processo contínuo: primeira rodada imediata e depois a cada 8 horas
python main.py
```

O fluxo contínuo considera apenas atividades publicadas, pendentes, com publicação e prazo
definidos, ainda não vencidas e cujo horário atual já alcançou a metade do intervalo entre
publicação e vencimento. Ele baixa e extrai materiais, monta o contexto, chama o provider,
salva a solução para revisão, gera os arquivos, envia-os ao Drive e anexa-os ao rascunho.
Não faz `turnIn` no Classroom. A execução é idempotente para o mesmo hash de contexto; mudanças no
enunciado ou nos materiais criam uma nova versão.

Os eventos normais são escritos em stdout e os erros em stderr. Em produção, o serviço systemd
envia ambos ao journald, permitindo acompanhar autenticação, sincronização, atividades elegíveis,
anexos, geração, upload, rascunhos, duração e próxima execução pelo Cockpit. O agente não depende
de arquivo `.log` separado. Para consultar diretamente no Ubuntu:

```bash
sudo journalctl -u classroom-agent.service -f
```

Os logs registram somente metadados operacionais. Chaves de API, tokens OAuth, arquivos de
credenciais e o conteúdo das respostas não são registrados.

## Management Platform — Fase 1: API privada read-only + run now

A API é um processo separado e usa o mesmo SQLite e o mesmo executor de ciclo do agente. Ela não
consulta o Google nos endpoints de leitura. O prefixo `/api/v1` mantém o contrato preparado para
evoluções do aplicativo Android.

```text
GET  /api/v1/health
GET  /api/v1/status
GET  /api/v1/history?limit=20&offset=0
GET  /api/v1/activities?limit=20
POST /api/v1/run
GET  /api/v1/assignments?limit=20&offset=0&course_id=1&status=NEW
GET  /api/v1/assignments/{assignment_id}
GET  /api/v1/assignments/{assignment_id}/solutions
GET  /api/v1/solutions/{solution_id}
GET  /api/v1/solutions/{solution_id}/artifacts
GET  /api/v1/artifacts/{artifact_id}
GET  /api/v1/artifacts/{artifact_id}/content
GET  /api/v1/settings
GET  /openapi.json
```

`limit` aceita valores de 1 a 100. `POST /api/v1/run` retorna `202` e inicia o mesmo ciclo usado
por `run --once` em uma thread de trabalho. Se o agente ou outra solicitação já estiver executando
um ciclo, retorna `409`. Um lock de arquivo do sistema operacional coordena os dois processos e é
liberado automaticamente se o processo terminar. Nenhum endpoint executa comandos arbitrários ou
faz `turnIn`.

O Core V1 permanece congelado. Os serviços `classroom-agent.service` (scheduler) e
`classroom-agent-api.service` (HTTP) continuam independentes. Todos os GETs usam dados locais;
nenhum GET sincroniza, resolve, gera documentos ou consulta Google/LLM. Somente `/run` pode
iniciar o pipeline existente, que pode sincronizar, gerar e enviar rascunhos conforme suas regras.
A revisão humana e o turn-in automático desativado continuam preservados.

Não há autenticação própria nesta fase: a fronteira de acesso é a tailnet/Tailscale Serve.
Quem recebe acesso à API pode ler enunciados, soluções e documentos acadêmicos e solicitar ciclos;
restrinja os membros autorizados na tailnet. Não abrir porta pública, não usar `0.0.0.0`, Funnel
ou Cloudflare. Swagger/ReDoc permanecem desativados; `/openapi.json` está disponível na rede privada
para conferir ou gerar modelos Kotlin. Nenhum código Android ou endpoint futuro foi criado.

Contrato e compatibilidade:

- IDs são inteiros locais positivos. Listagens retornam arrays. `limit` vai de 1 a 100;
  `offset` é não negativo e existe em history e assignments. Página vazia indica fim.
- `/activities` mantém seu significado de atividades recentes dos ciclos. `/assignments` lista
  todas as atividades persistidas, incluindo as sem ciclo/solution. Ordenação estável: presentes
  primeiro, depois ID decrescente. `course_id` é local e `status` filtra o estado de submissão
  persistido (`NEW`, `TURNED_IN` etc.), não o campo `status` normalizado (`PENDING`, `MISSING` etc.).
- Atraso, status normalizado e elegibilidade reutilizam funções do core sobre snapshots locais.
  Esses valores refletem o último sync e o relógio atual, não uma consulta ao Classroom.
- Detalhes incluem enunciado completo e metadados selecionados de attachments, sem arquivos
  baixados, URLs arbitrárias, payloads brutos ou dados de extração.
- Solutions vêm por versão decrescente; listas não carregam o texto de resposta. O DTO de revisão
  usa `review_kind`: `deliverable` para relatório acadêmico, `question_answer` para respostas
  curtas, `code` para arquivos e `unavailable` quando não existe conteúdo revisável seguro.
  Relatórios antigos sem deliverable não usam `answer` como fallback. Nas respostas curtas,
  `answer` e `question_answers` são explícitos; código usa `files`. Nunca são retornados
  understanding, prompts, reasoning, request_metadata ou resposta bruta do provider.
- Datas novas são ISO 8601 com timezone; UTC pode aparecer como `Z`. Datas legadas preservam
  sua representação com offset. Campos ausentes são `null`, não strings vazias.
- `/status` preserva campos anteriores e acrescenta `version`, `turn_in_enabled=false` e
  `last_cycle_result`. `next_cycle=null` significa que não há próxima execução registrada;
  a API não inventa um agendamento quando o scheduler está parado.
- Erros 404/409/422/500 usam `{"detail":"...","error":{"code":"...","message":"..."}}`.
  O `detail` de conflito 409 foi preservado. Validação 422 agora omite inputs e detalhes internos.
  Respostas de Range inválido são as respostas nativas de Starlette (416/400).
- Settings usa uma lista explícita de campos operacionais. Presença de token/credenciais não
  prova sessão Google válida: `google_authentication_status="not_verified"`. Nenhum token é lido
  ou atualizado pelo endpoint. `drive_folder_configured` indica configuração local, não saúde
  da integração. O intervalo mostrado é o da configuração carregada pelo processo da API;
  os dois serviços devem usar a mesma configuração.

Artifacts:

- O cliente fornece somente ID. O servidor resolve o registro, canonicaliza o caminho e exige
  arquivo regular dentro de `generated_root`, inclusive após resolver links simbólicos.
- Caminhos externos, arquivos ausentes, dotfiles e arquivos configurados de credenciais, token
  e banco não são servidos. Apenas artifacts READY/UPLOAD_PENDING/UPLOADED têm conteúdo disponível.
  Não existe endpoint genérico de filesystem, nem argumento de caminho para download.
- O nome de download é sintético (`artifact-ID.ext`), sem caminho absoluto ou nome interno.
  Metadados incluem tamanho quando o arquivo existe, versão da solution, versão do artifact,
  formato, estado e Drive file ID; nunca contêm path absoluto.
- PDF usa `application/pdf` e disposição inline; DOCX usa seu MIME oficial e attachment.
  TXT/Markdown e formatos textuais registrados usam MIME correspondente e attachment.
  `nosniff`, CSP sandbox e cache privado sem armazenamento acompanham os downloads.
- `FileResponse` fornece streaming e HTTP Range nativos: PDF parcial retorna 206 e Content-Range;
  intervalo fora do arquivo retorna 416. Não há implementação própria de streaming.
- A pasta de saída e o banco devem continuar graváveis somente pelos processos locais confiáveis.
  A API não aceita operações de escrita nesses arquivos. Arquivo ausente retorna 404 sem revelar path.

Os DTOs Pydantic ficam em `app/api_schemas.py`; a projeção de persistência para esses DTOs fica
em `app/management_api.py`. O Android só depende do contrato OpenAPI. Consultas de listas usam
joins/subconsultas agregadas e projeções JSON para evitar N+1 e carregar conteúdo de soluções.
O `busy_timeout=30000` existente foi preservado; WAL não foi ativado. Nenhuma migração ou
biblioteca nova foi instalada. Starlette, já transitiva do FastAPI, agora tem requisito explícito
`>=1.6,<2`, correspondente à versão validada para FileResponse/Range; instalações antigas devem
atualizar as dependências antes de usar este contrato. Testes usam SQLite temporário, fakes e bloqueio de rede externa
(loopback é permitido para os socket pairs internos do asyncio no Windows).

Exemplos sem dados pessoais (troque o endereço local pelo endereço privado do Serve no cliente):

```bash
curl 'http://127.0.0.1:8765/api/v1/assignments?limit=20&offset=0'
curl http://127.0.0.1:8765/api/v1/solutions/1
curl http://127.0.0.1:8765/api/v1/settings
curl -X POST http://127.0.0.1:8765/api/v1/run
curl -o revisao.pdf http://127.0.0.1:8765/api/v1/artifacts/1/content
curl -H 'Range: bytes=0-1023' http://127.0.0.1:8765/api/v1/artifacts/1/content
```

Exemplo de revisão acadêmica:

```json
{
  "id": 1, "assignment_id": 1, "version": 2, "status": "READY",
  "template": "academic-report", "provider": "mock", "model": "mock",
  "created_at": "2026-09-25T12:00:00Z", "updated_at": "2026-09-25T12:00:00Z",
  "has_deliverable": true, "artifact_count": 1, "review_kind": "deliverable",
  "deliverable": "# Relatório\nTexto final para revisão.", "answer": null,
  "question_answers": [], "files": []
}
```

Exemplo de artifact:

```json
{
  "id": 1, "solution_id": 1, "solution_version": 2, "version": 1,
  "format": "PDF", "name": "artifact-1.pdf", "size_bytes": 12345,
  "status": "READY", "created_at": "2026-09-25T12:00:00Z",
  "uploaded_at": null, "drive_file_id": null
}
```

Exemplo de recurso inexistente:

```json
{"detail":"solution_not_found","error":{"code":"solution_not_found","message":"solution_not_found"}}
```

Para testar somente a API local:

```powershell
.\.venv\Scripts\python.exe api_main.py
curl.exe http://127.0.0.1:8765/api/v1/health
curl.exe http://127.0.0.1:8765/api/v1/status
```

O bind padrão é exclusivamente loopback. Em produção, mantenha `API_HOST=127.0.0.1` e publique o
serviço apenas dentro da tailnet com Tailscale Serve:

```bash
sudo tailscale serve --bg http://127.0.0.1:8765
sudo tailscale serve status
```

Não use Tailscale Funnel para esta API. O exemplo de serviço separado está em
`deploy/classroom-agent-api.service.example`; uma falha ou reinício da API não interrompe o agente.

Na VM Ubuntu, revise os caminhos e o usuário do arquivo de exemplo antes de instalá-lo. A sequência
de atualização é:

```bash
cd /home/ubuntu/ClassroomAgent
git pull --ff-only
./.venv/bin/python -m pip install -e .
sudo install -m 0644 deploy/classroom-agent-api.service.example /etc/systemd/system/classroom-agent-api.service
sudo systemctl daemon-reload
sudo systemctl restart classroom-agent.service
sudo systemctl enable --now classroom-agent-api.service
curl --fail http://127.0.0.1:8765/api/v1/health
sudo tailscale serve --bg http://127.0.0.1:8765
sudo tailscale serve status
```

Para acompanhar os dois processos sem arquivos de log paralelos:

```bash
journalctl -u classroom-agent.service -f
journalctl -u classroom-agent-api.service -f
```

## Templates PDF

- `question-answer`: lista cada pergunta e sua resposta; é o padrão para questões objetivas ou curtas.
- `academic-report`: apresenta entendimento, resposta e itens de questões em formato de relatório.
- `code-assignment`: inclui cabeçalho acadêmico, resposta e blocos de código/especificações em fonte monoespaçada.

O template pode ser escolhido com `--template`; sem override, o builder escolhe pelo tipo estruturado da solução.
O formato físico pode ser definido independentemente com `--format pdf`, `--format docx`, `--format txt` ou `--format md`. Sem `--format`, o comportamento anterior permanece: gera o PDF de revisão e os arquivos adicionais solicitados pela atividade. DOCX é renderizado como documento Word real; TXT remove a formatação Markdown preservando texto, listas e código; MD mantém a sintaxe Markdown.

## Drive e organização por disciplina

Defina `DRIVE_RESPONSES_FOLDER_ID` com o ID da pasta raiz. Com `DRIVE_ORGANIZE_BY_COURSE=true`, o agente cria ou reutiliza uma subpasta com o nome da disciplina e envia os arquivos nela. Se o ID ficar vazio, o agente procura ou cria a pasta definida em `DRIVE_RESPONSES_FOLDER_NAME`. O upload é explícito e aceita apenas extensões textuais permitidas e PDFs gerados dentro de `GENERATED_ROOT`.

## Persistência e estrutura

As tabelas principais são `courses`, `assignments`, `submissions`, `sync_runs`, `attachments`, `solutions` e `generated_artifacts`. `solutions` guarda versões, provider/modelo, hash do contexto, resposta estruturada e estado de revisão. `generated_artifacts` guarda template, versão, caminho, hash, estado e metadados do Drive.

```text
app/
  auth/ classroom/ cli/ context/ domain/ documents/
  attachments/ llm/ persistence/ sync.py config.py
tests/
data/                 # banco, anexos e PDFs locais
main.py
.env.example
pyproject.toml
```

## Segurança

Nunca versione `.env`, `credentials*.json`, `token*.json`, `data/` ou arquivos pessoais. O `.gitignore` do projeto cobre esses padrões. Revise anexos como dados não confiáveis, mantenha os scopes mínimos e não compartilhe tokens, IDs pessoais, e-mails ou PDFs privados.

## Troubleshooting

- **OAuth/scopes:** remova ou renomeie o token definido em `GOOGLE_TOKEN_FILE` e execute `python main.py auth` ou `python main.py auth --write` conforme o erro. Confira se a credencial é Desktop app.
- **Drive API/upload:** habilite a Drive API, execute `auth --write`, valide `DRIVE_RESPONSES_FOLDER_ID` e confira os artifacts em `GENERATED_ROOT`. O upload explícito envia o PDF e os arquivos textuais pendentes.
- **Contexto sem anexos:** rode `sync`, confira `local-assignments` e depois `attachments`, `fetch-attachments` e `extract`. Materiais removidos ou não suportados permanecem registrados com estado.
- **Forms não acessível:** use `context ID --offline` para montar o contexto sem leitura da API de Forms; verifique API habilitada e permissões quando a leitura oficial for necessária.
- **Banco/configuração:** confira caminhos relativos ao diretório do projeto, permissões de escrita em `data/` e o timezone configurado.

## Testes e qualidade

```powershell
python -m ruff check .
python -m mypy app main.py
python -m pytest
```

Esses comandos verificam lint, tipos e a suíte presente no repositório. O README não fixa uma quantidade de testes.

## Limitações atuais

Não há `turnIn` automático no Classroom, envio automático de respostas para Google Forms nem browser automation. O fluxo contínuo anexa os arquivos ao rascunho para revisão; a decisão de entrega continua manual.

## Próxima fase

O próximo ciclo pode melhorar a experiência de revisão, ampliar observabilidade e cobertura de providers, adicionar mais formatos de documento e aprofundar integrações somente leitura, preservando o fluxo explícito e seguro de aprovação humana.
