import json
from abc import ABC, abstractmethod
from typing import Any

import requests

from app.config import Settings
from app.errors import AppError
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
        if settings.astra_model != "gpt-6-astra":
            raise AppError("ASTRA_MODEL suportado nesta fase: gpt-6-astra.")
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
                    raise AppError(
                        "Astra: requisição recusada/indisponível. Confira chave, acesso e quota."
                    )
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise AppError("Astra: resposta excede limite seguro.")
                result = json.loads(body)
            if result.get("status") != "completed":
                raise AppError("Astra: resposta incompleta; nenhuma solução pronta foi salva.")
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
        except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AppError(
                "Astra: falha de transporte/protocolo; tente novamente manualmente."
            ) from exc


def get_provider(settings: Settings, override: str | None = None) -> LLMProvider:
    name = override or settings.llm_provider
    if name == "mock":
        return MockProvider()
    if name == "astra":
        return AstraProvider(settings)
    raise AppError("LLM_PROVIDER desconhecido. Use mock ou astra.")
