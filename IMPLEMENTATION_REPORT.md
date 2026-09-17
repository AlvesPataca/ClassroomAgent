# IMPLEMENTATION REPORT

Data: 17/09/2026. Entrega: **Fase 1 — Foundation**.

Atualização OAuth: a correção posterior está documentada em
[OAUTH_FIX_REPORT.md](OAUTH_FIX_REPORT.md). Ela substitui a validação original de
igualdade de scopes, adiciona diagnóstico seguro e amplia a suíte para 69 testes.
Inclui a correção posterior para os nomes equivalentes `coursework.me.readonly`
e `student-submissions.me.readonly` retornados pelo Google.
O restante deste documento registra a entrega inicial.

Implementação concluída e verificada localmente. A aceitação com dados reais de
`pending` permanece pendente do OAuth da conta do titular. Nenhuma credencial real
foi usada, solicitada no chat ou criada como exemplo funcional.

## Arquivos criados

Todos os arquivos abaixo são novos; não havia implementação prévia no workspace.

| Arquivo | Finalidade |
|---|---|
| `main.py` | Entrada dos quatro comandos |
| `pyproject.toml` | Python >=3.11, pacote, dependências e ferramentas |
| `requirements.txt` | Instalação a partir do pyproject |
| `.gitignore` | Exclusão de secrets, tokens, ambiente e artefatos |
| `.env.example` | Timezone, caminhos, timeout e retries, sem secrets |
| `README.md` | Instalação, Google Cloud, OAuth, comandos e diagnóstico |
| `IMPLEMENTATION_REPORT.md` | Este relatório |
| `app/__init__.py` | Pacote da aplicação |
| `app/config.py` | Configuração validada, caminhos estáveis, três scopes readonly |
| `app/errors.py` | Erros seguros para apresentação |
| `app/auth/__init__.py`, `app/auth/google.py` | OAuth Desktop, token e renovação |
| `app/classroom/__init__.py`, `app/classroom/client.py` | API oficial v1, paginação, erros e retries |
| `app/classroom/mapper.py` | Mappers e conversão centralizada de prazos |
| `app/domain/__init__.py`, `app/domain/models.py` | Course, Assignment, Submission, enum de status |
| `app/domain/status.py` | Normalização e atraso calculado |
| `app/cli/__init__.py`, `app/cli/commands.py` | Typer, tabelas Rich e resultados parciais |
| `tests/test_domain.py` | Mappers, status, UTC, DST e limites de prazo |
| `tests/test_client.py` | Paginação, retries, erros e schema oficial sem rede |
| `tests/test_auth_config_cli.py` | OAuth, configuração, secrets e CLI |

Também foi criado `.gitignore` na raiz do workspace para excluir `work/` e
secrets. `work/` contém apenas ferramentas auxiliares e runtime de validação,
fora do projeto entregável. Nenhum commit foi criado.

A revisão automática bloqueou o comando final de limpeza opcional de build/cache
e auditoria de ignore via Git, por exigir uma aprovação indisponível na sessão.
Esses artefatos podem permanecer no diretório; estão cobertos pelo `.gitignore`.
O workspace não era um repositório Git; a auditoria `git check-ignore` não foi
concluída. As regras de ignore foram criadas, sem versionar qualquer secret.

## Dependências

Produção: google-api-python-client, google-auth, google-auth-oauthlib,
google-auth-httplib2, httplib2, oauthlib, Pydantic 2, python-dotenv, Typer, Rich e
tzdata. Desenvolvimento: ruff, mypy e pytest. Build: setuptools.

Versões verificadas nesta execução: Python 3.12.10, google-api-python-client
2.200.0, google-auth 2.58.0, google-auth-oauthlib 1.4.1, google-auth-httplib2
0.4.2, httplib2 0.32.0, oauthlib 3.3.1, Pydantic 2.13.5, python-dotenv 1.2.3,
Typer 0.27.2, Rich 14.3.4, tzdata 2026.4, ruff 0.16.8, mypy 1.20.2 e pytest
9.1.1. Os intervalos suportados estão no pyproject; não há lock de dependências.

## Testes executados e resultados

| Verificação | Resultado |
|---|---|
| `ruff check .` | Passou |
| `ruff format --check .` | Passou |
| `mypy` em modo strict | Passou; 14 arquivos de produção |
| `pytest -q` | 47 passaram |
| `compileall -q app main.py` | Passou |
| Imports de todos os módulos funcionais | Passaram |
| `main.py --help` e ajuda dos quatro comandos | Passaram |
| `auth` sem credenciais | Erro esperado, sem abrir navegador |
| `courses`, `assignments`, `pending` sem token | Erro esperado, orientando autenticação |
| Construção de wheel com setuptools | Passou |
| `pip check` | Nenhuma dependência quebrada |

A primeira rodada encontrou um teste de largura de terminal e ajustes de lint e
tipagem; todos foram corrigidos. Os testes usam dados artificiais exclusivamente
em `tests/`, nunca no cliente de produção.

Ambiente: o Python do PATH era um alias sem instalação e não havia runtime
empacotado disponível. Foi baixado o Python oficial embeddable para `work/python`
e instaladas as dependências declaradas. O sandbox Windows restringiu diretórios
temporários criados com modo 0700, então pip, pytest e build foram executados com
um harness local que preserva a herança das ACLs nesses diretórios. Isso não
altera o código entregue. O build editable pelo pip encontrou também o limite
de comprimento de caminhos do Windows; dependências foram instaladas diretamente
do pyproject e o wheel foi construído com sucesso no processo local. Instalação
editable numa instalação normal de Python não foi validada neste sandbox.

## Comandos e execução

Na pasta do projeto, com Python 3.11+:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
# Coloque o JSON OAuth Desktop em credentials.json.
python main.py auth
python main.py courses
python main.py assignments
python main.py pending
```

Opções: `--course ID_GOOGLE_DO_CURSO`, `--include-archived` e a opção global
`--debug`. O README contém exemplos, semântica de status e troubleshooting.

Necessário: projeto Google Cloud com Classroom API habilitada, consentimento
OAuth configurado e cliente Desktop. A conta precisa ser aluna dos cursos e ter
acesso autorizado pela instituição. O token local renova automaticamente quando
válido; não foi criado nesta execução. Timezone padrão: America/Sao_Paulo.

## Decisões e limites atuais

- Preservados os três scopes readonly exatos do escopo original.
- Cursos ativos por padrão; opção explícita para incluir arquivados.
- Paginação completa em cursos, atividades e submissões; consultas somente da
  conta autenticada. Payloads originais de Assignment/Submission em memória.
- UTC na origem, datetime aware no domínio e timezone configurado na exibição.
  Prazo incompleto é erro; ausência total significa sem prazo.
- PENDING/MISSING exigem estado conhecido de pendência. Submissão ausente é
  UNKNOWN; `late` da API fica separado do atraso calculado.
- RETURNED sem nota não implica reenvio; nota publicada zero é GRADED.
- IDs Google são utilizados nesta fase; IDs locais e campos de histórico de
  sincronização dependem da persistência da Fase 2.
- Falha por curso gera resultado parcial e saída 1; erros externos não expõem
  payloads, tokens ou credenciais, inclusive em `--debug`.
- Não há logs persistentes, criptografia de token, banco, Drive, anexos,
  LLM/Astra provider, soluções, entrega ou fases posteriores.
- A consulta de dados reais não foi executada. Python 3.11 é o mínimo declarado
  e alvo das verificações estáticas; a execução foi em Python 3.12.10.
- A documentação oficial confirma o identificador `gpt-6-astra`, mas as
  ferramentas desta tarefa não permitem confirmar ou trocar o modelo do turno
  já em execução. Não afirmo ter realizado uma troca de modelo nem criado outra
  tarefa no Work. A implementação foi feita no workspace desta tarefa.

Referências: [Google Classroom Python](https://developers.google.com/workspace/classroom/quickstart/python),
[CourseWork](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork),
[StudentSubmission](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork.studentSubmissions),
[GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra).

**Próxima etapa: Fase 2 — Persistência. Não implementada nesta entrega.**
