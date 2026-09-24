from app.errors import AppError

MESSAGES = {
    "GEMINI_BUSY": "Gemini HTTP 503: modelo com alta demanda. Tente novamente mais tarde.",
    "GEMINI_REGION": "Gemini HTTP 400: região/localização não suportada pela API.",
    "GEMINI_SCHEMA": "Gemini HTTP 400: API rejeitou o JSON Schema da solução.",
    "GEMINI_PARAMETER": "Gemini HTTP 400: parâmetro não suportado pelo modelo/endpoint.",
    "GEMINI_LIMIT": "Gemini HTTP 429: quota/limite atingido. Confira o Free Tier no AI Studio.",
    "GEMINI_AUTH": "Gemini: acesso recusado. Confira GEMINI_API_KEY e projeto no AI Studio.",
    "GEMINI_BLOCKED": "Gemini bloqueou a geração; nenhuma resposta foi salva.",
    "GEMINI_TIMEOUT": "Gemini: tempo limite. Ajuste GEMINI_TIMEOUT_SECONDS ou tente mais tarde.",
    "AUTH": "API HTTP 401: chave inválida/revogada. Confira ASTRA_API_KEY.",
    "ACCESS": "API HTTP 403/404: sem acesso ao modelo ou recurso não encontrado.",
    "QUOTA": "API: saldo/quota insuficiente. Confira faturamento do projeto da API.",
    "RATE": "API HTTP 429: limite de requisições/tokens. Tente mais tarde.",
    "REQUEST": "API HTTP 400: requisição ou schema rejeitado.",
    "SCHEMA": "API: JSON Schema rejeitado (invalid_json_schema).",
    "SERVER": "API indisponível ou erro HTTP inesperado. Tente mais tarde.",
    "TIMEOUT": "Tempo limite da API. Ajuste HTTP_TIMEOUT_SECONDS (por exemplo, 120).",
    "NETWORK": "Falha de conexão com a API. Confira rede/proxy/TLS.",
    "PROTOCOL": "Resposta da API em formato inesperado.",
    "INCOMPLETE": "API retornou resposta incompleta ou excedeu limite de saída.",
    "VALIDATION": "Resposta inválida após reparo: invalid_schema.",
    "UNKNOWN": "Falha de geração/validação não classificada; nenhuma resposta válida salva.",
}


class ProviderFailure(AppError):
    def __init__(self, code: str, *, validation_reason: str | None = None) -> None:
        self.code = code if code in MESSAGES else "UNKNOWN"
        self.validation_reason = None
        message = MESSAGES[self.code]
        if self.code == "VALIDATION" and validation_reason in {
            "invalid_json",
            "invalid_schema",
            "missing_deliverable",
            "empty_deliverable",
            "missing_answer",
            "empty_answer",
            "missing_understanding",
            "empty_understanding",
            "invalid_sources",
            "questions_inconsistent",
            "classification_template_inconsistent",
            "unsafe_response_content",
            "response_too_large",
            "missing_summary",
            "missing_assignment_types",
            "missing_question_answers",
            "missing_artifacts",
            "missing_assumptions",
            "missing_uncertainties",
            "missing_sources_used",
            "missing_requires_user_input",
            "missing_warnings",
            "other_validation_error",
        }:
            self.validation_reason = validation_reason
            message = f"Resposta inválida após reparo: {validation_reason}."
        super().__init__(message)
