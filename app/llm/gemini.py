"""Gemini Developer API, text generation only; billing tier belongs to the key's project."""

import json
import re
from typing import Any

import requests

from app.config import Settings
from app.errors import AppError
from app.llm.errors import ProviderFailure
from app.llm.providers import LLMProvider


def generation_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep generation grammar small; full limits remain enforced by local Pydantic."""

    def convert(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: convert(child)
                for key, child in value.items()
                if key not in {"minLength", "maxLength", "minItems", "maxItems", "title"}
            }
        if isinstance(value, list):
            return [convert(child) for child in value]
        return value

    result: dict[str, Any] = convert(schema)
    return result


def classify_bad_request(response: requests.Response) -> str:
    """Inspect a bounded error privately; return only local, allowlisted diagnostics."""
    body = bytearray()
    for chunk in response.iter_content(4096):
        body.extend(chunk)
        if len(body) > 65536:
            return "REQUEST"
    try:
        error = json.loads(body).get("error", {})
        message = str(error.get("message", "")).lower()
        reasons = {d.get("reason") for d in error.get("details", []) if isinstance(d, dict)}
        if reasons & {"API_KEY_INVALID", "API_KEY_EXPIRED"} or any(
            text in message for text in ("api key not valid", "api key expired", "key was reported")
        ):
            return "GEMINI_AUTH"
        if "location" in message or "region" in message:
            return "GEMINI_REGION"
        if "schema" in message:
            return "GEMINI_SCHEMA"
        if "not supported" in message or "unknown name" in message:
            return "GEMINI_PARAMETER"
    except (ValueError, TypeError, AttributeError):
        pass
    return "REQUEST"


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, settings: Settings) -> None:
        if not settings.gemini_api_key.get_secret_value():
            raise AppError("Configure GEMINI_API_KEY no .env com uma chave do Google AI Studio.")
        if not re.fullmatch(r"gemini-[a-z0-9.-]{1,65}", settings.gemini_model):
            raise AppError("GEMINI_MODEL inválido; use o identificador do modelo, sem URL.")
        self.model = settings.gemini_model
        self._key = settings.gemini_api_key
        self._timeout = settings.gemini_timeout_seconds

    def generate(self, system: str, data: str, schema: dict[str, Any]) -> str:
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": data}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "candidateCount": 1,
                "maxOutputTokens": 16000,
            },
        }
        try:
            with requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + self.model
                + ":generateContent",
                headers={"x-goog-api-key": self._key.get_secret_value()},
                json=payload,
                timeout=(10, self._timeout),
                stream=True,
                allow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    if response.status_code == 400:
                        raise ProviderFailure(classify_bad_request(response))
                    if response.status_code == 503:
                        raise ProviderFailure("GEMINI_BUSY")
                    if response.status_code == 429:
                        raise ProviderFailure("GEMINI_LIMIT")
                    if response.status_code in {401, 403}:
                        raise ProviderFailure("GEMINI_AUTH")
                    raise ProviderFailure(
                        {400: "REQUEST", 404: "ACCESS"}.get(response.status_code, "SERVER")
                    )
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise ProviderFailure("PROTOCOL")
                result = json.loads(body)
            if result.get("promptFeedback", {}).get("blockReason"):
                raise ProviderFailure("GEMINI_BLOCKED")
            candidates = result.get("candidates", [])
            if len(candidates) != 1:
                raise ProviderFailure("PROTOCOL")
            candidate = candidates[0]
            if candidate.get("finishReason") != "STOP":
                raise ProviderFailure("INCOMPLETE")
            parts = candidate.get("content", {}).get("parts", [])
            texts: list[str] = []
            for part in parts:
                if part.get("thought") is True:
                    continue
                if not isinstance(part.get("text"), str) or any(
                    k in part for k in ("functionCall", "executableCode", "codeExecutionResult")
                ):
                    raise ProviderFailure("PROTOCOL")
                texts.append(part["text"])
            if not texts:
                raise ProviderFailure("PROTOCOL")
            return "".join(texts)
        except requests.Timeout:
            raise ProviderFailure("GEMINI_TIMEOUT") from None
        except requests.RequestException:
            raise ProviderFailure("NETWORK") from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderFailure("PROTOCOL") from None
