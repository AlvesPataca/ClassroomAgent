# Correção OAuth — Fase 1

## Atualização após a captura do erro real

A primeira correção estava incompleta. A captura mostrou ausência apenas de
`classroom.coursework.me.readonly`, compatível com o Google retornar sua forma
`classroom.student-submissions.me.readonly`. Tratar apenas conjuntos maiores não
cobria essa canonicalização.

Agora a comparação reconhece exclusivamente esse par readonly do próprio aluno.
`classroom.courses.readonly` continua obrigatório separadamente. Scopes de escrita,
de outros alunos ou com nomes semelhantes não satisfazem essa regra. Não há
alteração dos três scopes enviados ao Google. `granted_scopes` salva somente o
conjunto efetivamente recebido, e a equivalência é aplicada separadamente na
validação do login, reload e refresh.

A documentação geral do Google explica que múltiplos nomes de scopes podem ser
retornados como um único nome. O catálogo Classroom descreve os dois nomes com a
mesma capacidade de leitura do aluno. A captura fornecida é consistente com essa
canonicalização; não houve nova autenticação na conta real nesta execução.
As autorizações por curso continuam sendo verificadas pelo servidor Classroom.

Fontes oficiais:
[Normalização de scopes no Google OAuth](https://developers.google.com/identity/protocols/oauth2#3.-examine-scopes-of-access-granted-by-the-user.),
[Catálogo de scopes Classroom](https://developers.google.com/workspace/classroom/guides/auth).

Verificação após esta atualização: **ruff check . passou; mypy . passou (19
arquivos); pytest passou com 69 testes**. Os testes adicionais percorrem o fluxo
OAuth real das bibliotecas com HTTP/navegador simulados, incluindo o retorno dos
dois scopes, persistência, reload, refresh e rejeição de permissões não equivalentes.

Arquivos desta atualização: `app/auth/google.py`, `tests/test_oauth_regression.py`,
`README.md`, `IMPLEMENTATION_REPORT.md` e este relatório. Nenhum secret real foi
lido ou alterado; Fase 2 permanece fora do escopo.

O relato abaixo documenta a correção anterior; sua exigência de todos os nomes
literais é substituída pela equivalência restrita descrita acima.

## Causa e reprodução

Foi reproduzido com as bibliotecas instaladas o fluxo callback → erro por diferença
de scopes → ausência de token local. O teste executa `run_local_server`,
`fetch_token`, o parser OAuthLib e a construção real de Credentials. Apenas
navegador, servidor local e transporte HTTP são substituídos por doubles de teste.

1. `run_local_server()` responde ao navegador **antes** da troca do código pelo
   token. Portanto a antiga mensagem “Autenticação concluída” era prematura.
2. O OAuthLib 3.3.1 compara scopes solicitados e retornados. Se forem diferentes,
   lança `Warning`, mesmo quando a resposta contém todos os scopes necessários e
   um adicional. O token já foi parseado, mas ainda não foi atribuído à sessão.
3. Nosso `except (OAuth2Error, Warning)` convertia esse aviso em “OAuth recusado
   ou permissões incompletas”. A execução nunca chegava a `save_token()`.
4. Havia ainda dois pontos independentes com igualdade exata: a validação de
   `granted_scopes` após o retorno e a validação de `scopes` no token local.
5. `credentials.scopes` e `has_scopes()` não são prova das permissões concedidas:
   nessa biblioteca consultam o conjunto solicitado. `to_json()` também não salva
   `granted_scopes` automaticamente.

Uma simples troca de `==` por `issubset` depois de `run_local_server()` não
resolveria o aviso lançado antes do retorno. A ordem dos scopes, isoladamente,
não era um problema nas comparações por conjunto; uma string tratada com `set()`
era incorreta, porque virava conjunto de caracteres. A normalização agora trata
strings, listas, ordenação, espaços e duplicatas.

A reprodução confirma o defeito e seu caminho até a mensagem relatada. Não foi
capturada a resposta da conta real; portanto não se afirma qual scope adicional
ou diferença específica o Google retornou naquele login. O novo diagnóstico
permite identificar isso sem revelar credenciais. Nenhum alias de scope foi
inventado: um scope obrigatório realmente ausente continua causando erro.

## Correção

- Mantidos, sem alteração, os três scopes readonly de `app/config.py`.
- `include_granted_scopes="false"` explícito na autorização. Não é usado como
  substituto da verificação do conjunto retornado.
- Recuperação restrita ao Warning de mudança de scopes: exige token parseado,
  scopes originais esperados, coerência entre `new_scope` e `token.scope` e presença
  de todos os scopes necessários. Só então atribui o token à sessão e constrói
  Credentials pelo próprio Google Auth OAuthlib.
- Não configura `OAUTHLIB_RELAX_TOKEN_SCOPE`, não desativa validação de state,
  TLS, erros de token ou permissões faltantes. Warning arbitrário é recusado.
- Validação prioriza `granted_scopes`; se omitido, usa o grant salvo ou o conjunto
  solicitado, conforme a regra de scope inalterado do OAuth 2.0 (RFC 6749 §5.1).
- Credenciais precisam estar válidas, não expiradas e possuir refresh token antes
  de salvar. Refresh também passa pela validação; grant incompleto não sobrescreve
  o token antigo.
- Persistência atômica preserva `granted_scopes` separadamente do campo `scopes`,
  porque a serialização nativa não preserva esse dado. Reload verifica o grant
  salvo, mantendo os scopes solicitados restritos aos três originais.
- Acesso extra preexistente não é solicitado ou usado para escrever no Google.
- Mensagem do navegador agora indica apenas callback recebido. O terminal
  confirma sucesso somente após validação e persistência.

## Diagnóstico e execução

```powershell
python main.py auth --debug
python main.py courses
```

Também funciona `python main.py --debug auth`.

O diagnóstico imprime apenas scopes, `credentials.valid`, `credentials.expired`,
presença booleana de refresh token e classe de erro. Não imprime tokens, código
OAuth, client secret, conteúdo de credentials.json, payload HTTP ou traceback.
Não habilita logs DEBUG dos SDKs, que poderiam expor dados sensíveis.

Se realmente faltarem permissões, a mensagem lista os scopes obrigatórios
ausentes e não grava token. Se não houver token do login anterior, basta executar
`auth --debug`; não há necessidade de apagar `credentials.json`.

## Compatibilidade e verificações

Código das bibliotecas instaladas revisado: google-auth **2.58.0**,
google-auth-oauthlib **1.4.1**, requests-oauthlib **2.0.0**, oauthlib **3.3.1**.
Nenhuma atualização de dependência foi necessária.

| Comando | Resultado |
|---|---|
| `ruff check .` | Passou |
| `mypy .` | Passou, 19 arquivos incluindo testes |
| `pytest -q` | 61 passaram |

Os testes novos cobrem a falha original antes da correção; grant adicional;
string/lista, espaços, ordem e duplicatas; scope omitido; persistência e reload;
permissão faltante; state incorreto; invalid_grant; perda de grant após refresh;
`has_scopes()` enganoso; Warning não reconhecido; e as duas posições de `--debug`
com verificação de ausência de secrets na saída.

Validação em Python 3.12.10, com o runtime local previamente preparado. Pytest
continua usando o harness de temporários do sandbox descrito no relatório inicial;
nenhuma alteração de ACL faz parte do projeto. Mypy agora verifica também corpos
dos testes e exclui cópias geradas em `build/`; produção continua em modo strict.

## Arquivos alterados/criados

Alterados:

- `app/auth/google.py`
- `app/cli/commands.py`
- `pyproject.toml`
- `tests/test_auth_config_cli.py`
- `tests/test_domain.py` (asserções de não-null e tipagem para `mypy .`)
- `README.md`
- `IMPLEMENTATION_REPORT.md`

Criados:

- `tests/__init__.py`
- `tests/test_oauth_regression.py`
- `OAUTH_FIX_REPORT.md`

Nenhum arquivo de credenciais ou token real foi lido, alterado ou versionado por
esta correção. **Fase 2 não implementada.**

Referências: [Google OAuth e autorização incremental](https://developers.google.com/identity/protocols/oauth2/web-server#incrementalAuth),
[OAuthLib: validação de token](https://github.com/oauthlib/oauthlib/blob/master/oauthlib/oauth2/rfc6749/parameters.py),
[Google Auth OAuthlib](https://googleapis.dev/python/google-auth-oauthlib/latest/reference/google_auth_oauthlib.flow.html).
