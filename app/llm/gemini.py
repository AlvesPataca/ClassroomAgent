"""Gemini Developer API, text generation only; billing tier belongs to the key's project."""

import json
import random
import re
import time
from typing import Any

import requests

from app.config import Settings
from app.errors import AppError
from app.llm.errors import ProviderFailure
from app.llm.providers import LLMProvider


def generation_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep generation grammar small; full limits remain enforced by local Pydantic.

    The REST structured-output validator is stricter than JSON Schema itself on
    some model deployments.  Inline Pydantic's local references so the request
    contains only a shallow schema made of the documented primitive keywords.
    """

    definitions = schema.get("$defs", {})

    def convert(value: Any, resolving: frozenset[str] = frozenset()) -> Any:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                name = reference.removeprefix("#/$defs/")
                target = definitions.get(name)
                if isinstance(target, dict) and name not in resolving:
                    return convert(target, resolving | {name})
            allowed = {"type", "properties", "required", "items", "enum"}
            output: dict[str, Any] = {}
            for key, child in value.items():
                if key not in allowed:
                    continue
                if key == "properties" and isinstance(child, dict):
                    output[key] = {
                        name: convert(property_schema, resolving)
                        for name, property_schema in child.items()
                    }
                else:
                    output[key] = convert(child, resolving)
            return output
        if isinstance(value, list):
            return [convert(child, resolving) for child in value]
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
        model_pattern = r"gemini-[a-z0-9.-]{1,65}"
        if not re.fullmatch(model_pattern, settings.gemini_model) or not re.fullmatch(
            model_pattern, settings.gemini_fallback_model
        ):
            raise AppError("GEMINI_MODEL inválido; use o identificador do modelo, sem URL.")
        self.model = settings.gemini_model
        self._models = list(
            dict.fromkeys(
                (
                    settings.gemini_model,
                    settings.gemini_fallback_model,
                    "gemini-3.6-flash",
                    "gemini-3.5-flash",
                )
            )
        )
        self._key = settings.gemini_api_key
        self._timeout = settings.gemini_timeout_seconds
        self._max_retries = settings.max_retries

    def generate(self, system: str, data: str, schema: dict[str, Any]) -> str:
        for model_index, model in enumerate(self._models):
            self.model = model
            busy_attempt = 0
            structured = True
            while busy_attempt <= self._max_retries:
                try:
                    return self._generate_once(
                        model, system, data, schema if structured else None
                    )
                except ProviderFailure as exc:
                    if exc.code == "GEMINI_SCHEMA" and structured:
                        # Some generateContent deployments reject JSON Schema even
                        # when the model supports JSON output. Keep responseMimeType
                        # and enforce the complete Pydantic schema locally instead.
                        structured = False
                        continue
                    last_attempt = busy_attempt == self._max_retries
                    final_model = model_index + 1 == len(self._models)
                    transient = exc.code in {"GEMINI_BUSY", "GEMINI_LIMIT", "ACCESS"}
                    exhausted_final = final_model and (
                        exc.code != "GEMINI_BUSY" or last_attempt
                    )
                    if not transient or exhausted_final:
                        raise
                    if exc.code != "GEMINI_BUSY" or last_attempt:
                        break
                    time.sleep(min(2**busy_attempt, 8) + random.uniform(0, 0.5))
                    busy_attempt += 1
        raise AssertionError("Unreachable")

    def _generate_once(
        self, model: str, system: str, data: str, schema: dict[str, Any] | None
    ) -> str:
        generation_config: dict[str, Any] = {
            "responseMimeType": "application/json",
            "maxOutputTokens": 16000,
        }
        if schema:
            generation_config["responseJsonSchema"] = generation_schema(schema)
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": data}]}],
            "generationConfig": generation_config,
        }
        try:
            with requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + model
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
