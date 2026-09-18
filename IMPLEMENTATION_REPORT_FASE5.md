# IMPLEMENTATION REPORT — FASE 5

## Entrega

O projeto canônico continua sendo esta pasta `classroom-agent-fase4`; a Fase 5 foi incorporada nela sem criar uma nova cópia. O `DocumentBuilder` transforma a solução estruturada em PDF determinístico com três templates: `question-answer`, `academic-report` e `code-assignment`. A escolha usa `assignment_types`, com fallback conservador para `QUESTION_ANSWER`, e pode ser substituída pela CLI.

Os layouts seguem os três trabalhos de referência registrados na conversa: identificação compacta e exercícios para programação; cabeçalho `ATIVIDADE`, data, aluno, matéria e sequência numerada para respostas curtas; e hierarquia textual, blocos monoespaçados e paginação para trabalhos longos. O PDF usa A4, margens de 2 cm, tipografia legível, Unicode, wrapping de parágrafos/código e rodapé numerado.

## Persistência e segurança

`generated_artifacts` registra solução, atividade, template, versão, caminho interno, SHA-256, status, IDs/link do Drive, timestamps e erro sanitizado. Cada regeneração cria nova versão. O caminho é sempre `data/generated/<course-local-id>/<assignment-local-id>/`; nomes são sanitizados contra traversal, caracteres inválidos, nomes reservados e colisões.

O uploader só aceita PDFs dentro de `generated_root`, rejeita arquivos sensíveis e recebe o artifact validado pelo banco. `DRIVE_RESPONSES_FOLDER_ID` é preferencial; sem ele, `Classroom Agent - Respostas` é criada e, opcionalmente, uma subpasta por disciplina. A escrita usa `drive.file`; tokens antigos continuam válidos para leitura e pedem reautenticação explícita quando `upload` é usado.

## Comandos

```text
python main.py generate <assignment_local_id>
python main.py generate <id> --template question-answer
python main.py generate <id> --solution-version 2
python main.py artifacts <assignment_local_id>
python main.py upload <assignment_local_id>
python main.py generate <id> --upload
```

`generate` só aceita soluções `READY`, `NEEDS_REVIEW` ou `APPROVED`; não há envio ao Classroom.

## Validação

`pytest -q`: 293 testes regressivos passaram. `python -m compileall app main.py` passou. Smoke test adicional gerou PDF com acentos portugueses, resposta multipágina e código longo; o SHA-256 persistido coincidiu com o arquivo. A dependência de renderização é `reportlab` e a verificação visual local pode ser feita com `pdftoppm -png <arquivo.pdf> work/pdf-page`.

## Teste real documentado

1. Copie `.env.example` para `.env`, preencha `STUDENT_NAME` e, de preferência, `DRIVE_RESPONSES_FOLDER_ID`.
2. Remova/renomeie o token antigo e execute `python main.py auth` para conceder o escopo `drive.file` junto aos escopos de leitura.
3. Escolha uma atividade com solução pronta e rode `python main.py generate <id>`.
4. Abra o caminho mostrado, revise o PDF e liste o artifact com `python main.py artifacts <id>`.
5. Rode `python main.py upload <id>` e abra o `webViewLink` retornado para conferir a pasta controlada do Drive.

Não foram feitos uploads reais durante os testes automatizados.
