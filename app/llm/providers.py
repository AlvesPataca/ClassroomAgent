import json
from abc import ABC, abstractmethod
from typing import Any

import requests

from app.config import Settings
from app.errors import AppError
from app.llm.errors import ProviderFailure
from app.llm.schema import Solution


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def generate(self, system: str, data: str, schema: dict[str, Any]) -> str:
        """Return JSON text. No credentials/context records/Google tools in interface."""


class MockProvider(LLMProvider):
    name = "mock"
    model = "mock-v1"

    def generate(self, system: str, data: str, schema: dict[str, Any]) -> str:
        context = json.loads(data)["context"]
        return Solution(
            summary="SIMULAÇÃO MOCK — fluxo validado; nenhuma resolução acadêmica gerada.",
            assignment_types=context["assignment_types"],
            understanding="Contexto recebido para testar preparação e revisão humana.",
            answer="MOCK: configure o provider Astra para preparar uma resposta real.",
            deliverable="",
            question_answers=[],
            artifacts=[],
            assumptions=[],
            uncertainties=["Provider mock não resolve a atividade."],
            sources_used=["assignment.description"],
            requires_user_input=True,
            warnings=["SIMULAÇÃO; NÃO É UMA SOLUÇÃO ACADÊMICA."],
        ).model_dump_json()


class AstraProvider(LLMProvider):
    """Documented OpenAI Responses API; fixed official origin and no tools/redirects."""

    name = "astra"

    def __init__(self, settings: Settings) -> None:
        if settings.astra_base_url.rstrip("/") != "https://api.openai.com/v1":
            raise AppError(
                "ASTRA_BASE_URL deve ser https://api.openai.com/v1 (protocolo documentado)."
            )
        if settings.astra_model not in {"gpt-6-astra", "gpt-5-mini"}:
            raise AppError("ASTRA_MODEL suportado: gpt-5-mini ou gpt-6-astra.")
        if not settings.astra_api_key.get_secret_value():
            raise AppError(
                "Configure ASTRA_API_KEY no .env; acesso do Work não autentica a API local."
            )
        self.model = settings.astra_model
        self._key = settings.astra_api_key
        self._timeout = settings.http_timeout_seconds

    def generate(self, system: str, data: str, schema: dict[str, Any]) -> str:
        payload = {
            "model": self.model,
            "store": False,
            "tools": [],
            "instructions": system,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": data}]}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "assignment_solution",
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": 16000,
        }
        try:
            with requests.post(
                "https://api.openai.com/v1/responses",
                json=payload,
                headers={"Authorization": "Bearer " + self._key.get_secret_value()},
                timeout=(10, self._timeout),
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code != 200:
                    status = response.status_code
                    code = {
                        400: "REQUEST",
                        401: "AUTH",
                        403: "ACCESS",
                        404: "ACCESS",
                        429: "RATE",
                    }.get(status, "SERVER")
                    error_body = bytearray()
                    for chunk in response.iter_content(4096):
                        error_body.extend(chunk)
                        if len(error_body) > 65536:
                            break
                    if len(error_body) <= 65536:
                        try:
                            remote = json.loads(error_body).get("error", {}).get("code")
                            if remote in {"insufficient_quota", "billing_hard_limit_reached"}:
                                code = "QUOTA"
                            elif remote == "invalid_json_schema":
                                code = "SCHEMA"
                        except (ValueError, AttributeError, TypeError):
                            pass
                    raise ProviderFailure(code)
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise AppError("Astra: resposta excede limite seguro.")
                result = json.loads(body)
            if result.get("status") != "completed":
                raise ProviderFailure("INCOMPLETE")
            output: list[str] = []
            for item in result.get("output", []):
                if item.get("type") == "reasoning":
                    continue
                if item.get("type") != "message" or item.get("role") != "assistant":
                    raise AppError("Astra: saída inesperada; nenhuma ação executada.")
                for part in item.get("content", []):
                    if part.get("type") != "output_text":
                        raise AppError("Astra: recusa ou conteúdo inesperado.")
                    output.append(part["text"])
            if len(output) != 1:
                raise AppError("Astra: resposta estruturada ausente ou ambígua.")
            return output[0]
        except requests.Timeout:
            raise ProviderFailure("TIMEOUT") from None
        except requests.RequestException:
            raise ProviderFailure("NETWORK") from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderFailure("PROTOCOL") from None


def get_provider(settings: Settings, override: str | None = None) -> LLMProvider:
    name = override or settings.llm_provider
    if name == "mock":
        return MockProvider()
    if name == "astra":
        return AstraProvider(settings)
    if name == "gemini":
        from app.llm.gemini import GeminiProvider

        return GeminiProvider(settings)
    raise AppError("LLM_PROVIDER desconhecido. Use mock, astra ou gemini.")
