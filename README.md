# Classroom Agent

Agente local em Python para consultar o Google Classroom como aluno, sincronizar dados para SQLite, descobrir contexto de atividades, preparar soluções estruturadas com LLM, gerar PDFs revisáveis e, na Fase 5, enviar esses PDFs para uma pasta controlada do Google Drive.

## Visão geral e fluxo

`Google Classroom → sync/persistência SQLite → context discovery → LLM/solução estruturada → DocumentBuilder → PDF → Google Drive`

O Classroom fornece cursos, atividades, submissões e referências a materiais. `sync` mantém um snapshot local; a descoberta de contexto reúne descrição, links, Forms e anexos extraídos com provenance, limites e sanitização; `solve` grava uma resposta estruturada versionada; `generate` escolhe um template e cria um PDF; `upload` envia somente esse PDF para a pasta de respostas configurada.

## Funcionalidades por fase

- **Fase 1:** cliente Classroom somente leitura, autenticação OAuth Desktop, listagem de cursos e atividades, status de submissões, tratamento de prazos e configuração por `.env`.
- **Fase 2:** sincronização incremental e idempotente em SQLite, preservação de snapshots e payloads, tombstones para registros removidos, anexos persistidos e leitura local.
- **Fase 3:** descoberta, download seguro e extração local de anexos suportados, com limites de tamanho, hash, estados, histórico e proteção contra conteúdo não confiável.
- **Fase 4:** descoberta de contexto canônico para atividades, leitura opcional de Google Forms, provenance, orçamento de contexto, soluções estruturadas validadas por schema, providers `mock`, Gemini e Astra/OpenAI-compatible, versionamento e comparação por hash.
- **Fase 5:** `DocumentBuilder` com PDFs versionados, templates `QUESTION_ANSWER`, `ACADEMIC_REPORT` e `CODE_ASSIGNMENT`, persistência de artifacts, organização no Drive por disciplina e upload explícito com scope mínimo `drive.file`.

## Arquitetura

`app/classroom` acessa a API do Classroom; `app/sync.py` coordena a sincronização; `app/persistence` define banco, modelos e leitor local; `app/attachments` descobre, baixa e extrai materiais; `app/context` monta o contexto seguro; `app/llm` chama o provider e valida a solução; `app/documents` renderiza e envia PDFs; `app/cli` expõe a interface Typer.

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
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Credencial e modelo Gemini, quando usado. |
| `ASTRA_API_KEY`, `ASTRA_BASE_URL`, `ASTRA_MODEL` | Provider compatível com API OpenAI, quando usado. |
| `CONTEXT_MAX_CHARS` | Limite do contexto montado. |
| `DRIVE_RESPONSES_FOLDER_ID` | ID da pasta raiz controlada para respostas; vazio cria/encontra `DRIVE_RESPONSES_FOLDER_NAME`. |
| `DRIVE_RESPONSES_FOLDER_NAME` | Nome usado quando o ID não é informado. |
| `DRIVE_ORGANIZE_BY_COURSE` | Quando `true`, cria uma subpasta por disciplina. |

Não coloque secrets no `.env.example`; use valores locais somente no `.env`.

## Google Cloud, OAuth e scopes

Habilite as APIs necessárias e crie credenciais OAuth Desktop. O fluxo de leitura solicita apenas:

- `classroom.courses.readonly`;
- `classroom.coursework.me.readonly`;
- `classroom.student-submissions.me.readonly`;
- `drive.readonly`.

Para a Fase 5, o upload solicita adicionalmente somente `drive.file`, que permite criar e gerenciar os arquivos criados pelo agente. Execute `python main.py auth --write` quando precisar conceder esse scope. Se os scopes mudarem, renomeie ou remova apenas o arquivo indicado por `GOOGLE_TOKEN_FILE` e reautentique; não remova `credentials.json`.

## Autenticação

```powershell
python main.py auth
python main.py auth --write
```

O primeiro comando abre o navegador para os scopes de leitura. O segundo inclui `drive.file`. Comandos de leitura reutilizam e renovam o token local; nenhum token é impresso pelo CLI.

## CLI

Consulte sempre a ajuda instalada com `python main.py --help`. Os comandos atualmente disponíveis são:

```text
auth [--write]
sync
courses [--include-archived] [--api]
assignments [--course ID] [--include-archived] [--api]
pending [--course ID] [--include-archived] [--api]
local-assignments
attachments ASSIGNMENT_LOCAL_ID
fetch-attachments ASSIGNMENT_LOCAL_ID
extract ASSIGNMENT_LOCAL_ID
context ASSIGNMENT_LOCAL_ID [--offline] [--json]
solve ASSIGNMENT_LOCAL_ID [--provider PROVIDER] [--offline]
solutions ASSIGNMENT_LOCAL_ID [--offline]
solution SOLUTION_ID
generate ASSIGNMENT_LOCAL_ID [--template TEMPLATE] [--solution-version N] [--upload]
artifacts ASSIGNMENT_LOCAL_ID
upload ASSIGNMENT_LOCAL_ID
```

`--api` consulta o Classroom diretamente; sem ele, os comandos de consulta usam o snapshot local. IDs de anexos e atividades usados por `attachments`, `context`, `solve`, `generate` e `upload` são IDs inteiros locais, exibidos por `local-assignments`.

### Fluxo completo

```powershell
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
python main.py artifacts 12
python main.py auth --write
python main.py upload 12
```

Substitua `12` pelo ID local mostrado no seu banco. O provider `mock` não chama serviço externo e é útil para verificar o fluxo.

## Templates PDF

- `question-answer`: lista cada pergunta e sua resposta; é o padrão para questões objetivas ou curtas.
- `academic-report`: apresenta entendimento, resposta e itens de questões em formato de relatório.
- `code-assignment`: inclui cabeçalho acadêmico, resposta e blocos de código/especificações em fonte monoespaçada.

O template pode ser escolhido com `--template`; sem override, o builder escolhe pelo tipo estruturado da solução.

## Drive e organização por disciplina

Defina `DRIVE_RESPONSES_FOLDER_ID` com o ID da pasta raiz. Com `DRIVE_ORGANIZE_BY_COURSE=true`, o agente cria ou reutiliza uma subpasta com o nome da disciplina e envia o PDF nela. Se o ID ficar vazio, o agente procura ou cria a pasta definida em `DRIVE_RESPONSES_FOLDER_NAME`. O upload é explícito e só aceita PDFs gerados dentro de `GENERATED_ROOT`.

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
- **Drive API/upload:** habilite a Drive API, conceda `drive.file`, valide `DRIVE_RESPONSES_FOLDER_ID` e confira se o PDF está em `GENERATED_ROOT`.
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

Não há turn-in automático no Classroom, envio automático de respostas para Google Forms nem browser automation. O upload envia somente o PDF gerado para o Drive; revisão e decisão de entrega continuam manuais.

## Próxima fase

O próximo ciclo pode melhorar a experiência de revisão, ampliar observabilidade e cobertura de providers, adicionar mais formatos de documento e aprofundar integrações somente leitura, preservando o fluxo explícito e seguro de aprovação humana.
