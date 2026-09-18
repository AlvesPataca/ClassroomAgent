import json
from unittest.mock import patch

import pytest
import requests
from pydantic import SecretStr

from app.config import Settings
from app.llm.errors import ProviderFailure
from app.llm.providers import AstraProvider


@pytest.mark.parametrize("status,remote,expected", [
    (401, "invalid_api_key", "AUTH"),
    (429, "rate_limit_exceeded", "RATE"),
    (429, "insufficient_quota", "QUOTA"),
    (400, "invalid_json_schema", "SCHEMA"),
    (500, "unknown", "SERVER"),
])
def test_http_error_is_classified_without_echo(status, remote, expected):
    provider = AstraProvider(Settings(astra_api_key=SecretStr("SECRET")))
    with patch("app.llm.providers.requests.post") as post:
        response = post.return_value.__enter__.return_value
        response.status_code = status
        response.iter_content.return_value = [json.dumps({
            "error": {"code": remote, "message": "SECRET"}
        }).encode()]
        with pytest.raises(ProviderFailure) as error:
            provider.generate("system", "data", {})
    assert error.value.code == expected
    assert "SECRET" not in str(error.value)


def test_timeout_is_actionable_without_echo():
    provider = AstraProvider(Settings(astra_api_key=SecretStr("SECRET")))
    with patch("app.llm.providers.requests.post", side_effect=requests.Timeout("SECRET")):
        with pytest.raises(ProviderFailure) as error:
            provider.generate("system", "data", {})
    assert error.value.code == "TIMEOUT"
    assert "SECRET" not in str(error.value)
