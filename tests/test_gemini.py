import json
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from app.config import Settings, load_settings
from app.errors import AppError
from app.llm.errors import ProviderFailure
from app.llm.gemini import GeminiProvider, classify_bad_request, generation_schema
from app.llm.providers import get_provider
from app.llm.schema import Solution


def provider():
    return GeminiProvider(Settings(gemini_api_key=SecretStr("SECRET")))


def test_config(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET")
    settings = load_settings(tmp_path)
    assert isinstance(get_provider(settings), GeminiProvider)
    assert "SECRET" not in settings.model_dump_json() + repr(settings)
    with pytest.raises(AppError, match="GEMINI_API_KEY"):
        GeminiProvider(Settings())


def test_transport():
    with patch("app.llm.gemini.requests.post") as post:
        response = post.return_value.__enter__.return_value
        response.status_code = 200
        response.iter_content.return_value = [json.dumps({"candidates": [{
            "finishReason": "STOP", "content": {"parts": [{"text": '{"answer":"ok"}'}]}
        }]}).encode()]
        assert provider().generate("SYSTEM", "UNTRUSTED", Solution.model_json_schema()) == (
            '{"answer":"ok"}'
        )
        args = post.call_args
        assert args.args[0].endswith("/gemini-3.8-flash:generateContent")
        assert "SECRET" not in args.args[0] + json.dumps(args.kwargs["json"])
        assert args.kwargs["headers"] == {"x-goog-api-key": "SECRET"}
        assert not args.kwargs["allow_redirects"]
        payload = args.kwargs["json"]
        assert "tools" not in payload
        assert "candidateCount" not in payload["generationConfig"]
        assert payload["generationConfig"]["responseJsonSchema"]["type"] == "object"
        assert payload["systemInstruction"]["parts"][0]["text"] == "SYSTEM"


def test_generation_schema_inlines_pydantic_definitions():
    schema = generation_schema(Solution.model_json_schema())
    encoded = json.dumps(schema)
    assert "$defs" not in encoded and "$ref" not in encoded
    assert "additionalProperties" not in encoded and "maxLength" not in encoded
    assert schema["properties"]["question_answers"]["items"]["type"] == "object"


@pytest.mark.parametrize("status,code", [(429, "GEMINI_LIMIT"), (403, "GEMINI_AUTH"),
    (400, "REQUEST"), (500, "SERVER")])
def test_errors(status, code):
    with patch("app.llm.gemini.requests.post") as post:
        response = post.return_value.__enter__.return_value
        response.status_code = status
        response.text = "SECRET"
        with pytest.raises(ProviderFailure) as error:
            provider().generate("SYSTEM", "DATA", {})
    assert error.value.code == code and "SECRET" not in str(error.value)


@pytest.mark.parametrize("result", [
    {"promptFeedback": {"blockReason": "SAFETY"}},
    {"candidates": [{"finishReason": "MAX_TOKENS"}]},
    {"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"functionCall": {"name": "submit"}}]}}]},
])
def test_reject_partial_or_tools(result):
    with patch("app.llm.gemini.requests.post") as post:
        response = post.return_value.__enter__.return_value
        response.status_code = 200
        response.iter_content.return_value = [json.dumps(result).encode()]
        with pytest.raises(ProviderFailure):
            provider().generate("SYSTEM", "DATA", {})


@pytest.mark.parametrize("message,code", [
    ("API key not valid. SECRET", "GEMINI_AUTH"),
    ("response schema too complex SECRET", "GEMINI_SCHEMA"),
    ("User location is not supported SECRET", "GEMINI_REGION"),
    ("Unknown name field SECRET", "GEMINI_PARAMETER"),
    ("unrecognized SECRET", "REQUEST"),
])
def test_bad_request_diagnostic_does_not_echo(message, code):
    from unittest.mock import MagicMock

    response = MagicMock()
    response.iter_content.return_value = [json.dumps({"error": {"message": message}}).encode()]
    assert classify_bad_request(response) == code


def test_busy_response_is_retried():
    client = provider()
    with (
        patch.object(
            client,
            "_generate_once",
            side_effect=[ProviderFailure("GEMINI_BUSY"), '{"answer":"ok"}'],
        ) as generate,
        patch("app.llm.gemini.time.sleep") as sleep,
    ):
        assert client.generate("SYSTEM", "DATA", {}) == '{"answer":"ok"}'
    assert generate.call_count == 2
    sleep.assert_called_once()


def test_persistent_busy_response_uses_fallback_model():
    client = GeminiProvider(
        Settings(gemini_api_key=SecretStr("SECRET"), max_retries=1)
    )
    with (
        patch.object(
            client,
            "_generate_once",
            side_effect=[
                ProviderFailure("GEMINI_BUSY"),
                ProviderFailure("GEMINI_BUSY"),
                '{"answer":"fallback"}',
            ],
        ) as generate,
        patch("app.llm.gemini.time.sleep"),
    ):
        assert client.generate("SYSTEM", "DATA", {}) == '{"answer":"fallback"}'
    assert [call.args[0] for call in generate.call_args_list] == [
        "gemini-3.8-flash",
        "gemini-3.8-flash",
        "gemini-3.7-flash",
    ]
    assert client.model == "gemini-3.7-flash"


def test_rejected_remote_schema_retries_with_local_validation_only():
    client = provider()
    with patch.object(
        client,
        "_generate_once",
        side_effect=[ProviderFailure("GEMINI_SCHEMA"), '{"answer":"ok"}'],
    ) as generate:
        assert client.generate("SYSTEM", "DATA", {"type": "object"}) == '{"answer":"ok"}'
    assert generate.call_args_list[0].args[3] == {"type": "object"}
    assert generate.call_args_list[1].args[3] is None
