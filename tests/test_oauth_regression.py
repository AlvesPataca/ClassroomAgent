import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.util import setup_testing_defaults

import pytest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from requests import PreparedRequest, Response
from requests_oauthlib import OAuth2Session
from typer.testing import CliRunner

from app.auth.google import authenticate, normalize_scopes, validate_credentials
from app.cli.commands import app
from app.config import SCOPES, Settings
from app.errors import AppError

EXTRA = "openid"
SECRET_ACCESS = "test-access-do-not-log"
SECRET_REFRESH = "test-refresh-do-not-log"
SECRET_CLIENT = "test-client-secret-do-not-log"
CONFIG: dict[str, Any] = {
    "installed": {
        "client_id": "test-client",
        "client_secret": SECRET_CLIENT,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}


def configure(tmp_path: Path) -> Settings:
    settings = Settings(
        credentials_file=tmp_path / "credentials.json", token_file=tmp_path / "token.json"
    )
    settings.credentials_file.write_text(json.dumps(CONFIG), encoding="utf-8")
    return settings


def token_response(scopes: object, *, error: bool = False) -> Response:
    payload: dict[str, Any] = {
        "access_token": SECRET_ACCESS,
        "refresh_token": SECRET_REFRESH,
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    if scopes is not None:
        payload["scope"] = scopes
    if error:
        payload = {"error": "invalid_grant", "error_description": SECRET_ACCESS}
    response = Response()
    response.status_code = 400 if error else 200
    response._content = json.dumps(payload).encode()
    response.request = PreparedRequest()
    response.request.prepare(method="POST", url=CONFIG["installed"]["token_uri"])
    return response


def local_server_mocks(*, wrong_state: bool = False) -> Any:
    """Exercise real run_local_server/fetch_token/parser; replace only browser/socket/HTTP."""
    captured = {}
    server = MagicMock(server_port=8087)

    def make_server(host, port, wsgi_app, **kwargs):
        captured["app"] = wsgi_app
        return server

    def browser_open(url, **kwargs):
        captured["query"] = parse_qs(urlsplit(url).query)
        return True

    def callback():
        environ: dict[str, Any] = {}
        setup_testing_defaults(environ)
        state = "wrong" if wrong_state else captured["query"]["state"][0]
        environ["QUERY_STRING"] = urlencode({"state": state, "code": "test-code-do-not-log"})
        captured["message"] = b"".join(captured["app"](environ, MagicMock())).decode()

    server.handle_request.side_effect = callback
    return captured, make_server, browser_open


def test_original_library_raises_after_callback_for_extra_scope():
    captured, make_server, browser_open = local_server_mocks()
    flow = InstalledAppFlow.from_client_config(CONFIG, scopes=SCOPES)
    with (
        patch(
            "google_auth_oauthlib.flow.wsgiref.simple_server.make_server", side_effect=make_server
        ),
        patch("google_auth_oauthlib.flow.webbrowser.get") as browser,
        patch.object(
            OAuth2Session, "request", return_value=token_response(" ".join([*SCOPES, EXTRA]))
        ),
        pytest.raises(Warning) as caught,
    ):
        browser.return_value.open.side_effect = browser_open
        flow.run_local_server(port=0, authorization_prompt_message=None, success_message="callback")
    assert captured["message"] == "callback"
    assert hasattr(caught.value, "token")
    assert flow.oauth2session.token == {}


@pytest.mark.parametrize(
    "scopes",
    [
        " ".join([EXTRA, *reversed(SCOPES)]),
        list(reversed(SCOPES)),
        "  " + "   ".join([*SCOPES, SCOPES[0]]) + "  ",
        None,
        # Google canonicalizes the requested coursework scope to this name.
        " ".join([SCOPES[0], SCOPES[2], *SCOPES[3:]]),
        " ".join([SCOPES[0], SCOPES[1], *SCOPES[3:]]),
    ],
)
def test_real_flow_persists_valid_grant_and_reloads(tmp_path, scopes):
    settings = configure(tmp_path)
    captured, make_server, browser_open = local_server_mocks()
    diagnostics: list[str] = []
    with (
        patch(
            "google_auth_oauthlib.flow.wsgiref.simple_server.make_server", side_effect=make_server
        ),
        patch("google_auth_oauthlib.flow.webbrowser.get") as browser,
        patch.object(OAuth2Session, "request", return_value=token_response(scopes)),
    ):
        browser.return_value.open.side_effect = browser_open
        credentials = authenticate(settings, interactive=True, diagnostic=diagnostics.append)
    assert credentials.valid
    assert settings.token_file.exists()
    assert "Confira o resultado" in captured["message"]
    assert captured["query"]["scope"][0].split() == list(SCOPES)
    assert captured["query"]["include_granted_scopes"] == ["false"]
    saved = json.loads(settings.token_file.read_text())
    expected = normalize_scopes(scopes if scopes is not None else SCOPES)
    assert set(saved["granted_scopes"]) == expected
    with patch.object(OAuth2Session, "request", side_effect=AssertionError("Unexpected network")):
        assert authenticate(settings, diagnostic=diagnostics.append).valid
    output = "\n".join(diagnostics)
    for secret in (SECRET_ACCESS, SECRET_REFRESH, SECRET_CLIENT, "test-code-do-not-log"):
        assert secret not in output
    for label in (
        "scopes requeridos",
        "scopes concedidos",
        "credentials.valid: True",
        "credentials.expired: False",
        "refresh token presente: True",
    ):
        assert label in output


@pytest.mark.parametrize("scenario", ["missing_scope", "wrong_state", "invalid_grant"])
def test_flow_still_rejects_security_failures_without_persisting(tmp_path, scenario):
    settings = configure(tmp_path)
    captured, make_server, browser_open = local_server_mocks(wrong_state=scenario == "wrong_state")
    scopes = SCOPES[:1] if scenario == "missing_scope" else SCOPES
    with (
        patch(
            "google_auth_oauthlib.flow.wsgiref.simple_server.make_server", side_effect=make_server
        ),
        patch("google_auth_oauthlib.flow.webbrowser.get") as browser,
        patch.object(
            OAuth2Session,
            "request",
            return_value=token_response(" ".join(scopes), error=scenario == "invalid_grant"),
        ),
        pytest.raises(AppError) as caught,
    ):
        browser.return_value.open.side_effect = browser_open
        authenticate(settings, interactive=True)
    assert not settings.token_file.exists()
    assert SECRET_ACCESS not in str(caught.value)
    assert captured["message"]


def test_has_scopes_is_not_evidence_of_actual_grant():
    credentials = Credentials(
        SECRET_ACCESS,
        scopes=SCOPES,
        granted_scopes=SCOPES[:1],
        refresh_token=SECRET_REFRESH,
        expiry=(datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None),
    )
    assert credentials.has_scopes(SCOPES)
    with pytest.raises(AppError, match="Scopes obrigatórios"):
        validate_credentials(credentials, None)


def test_cached_grant_cannot_be_hidden_by_requested_scopes(tmp_path):
    settings = configure(tmp_path)
    settings.token_file.write_text(json.dumps({"scopes": SCOPES, "granted_scopes": SCOPES[:1]}))
    with pytest.raises(AppError, match="Scopes obrigatórios"):
        authenticate(settings)


def test_refresh_grant_loss_does_not_overwrite_token(tmp_path):
    settings = configure(tmp_path)
    credentials = Credentials(
        SECRET_ACCESS,
        scopes=SCOPES,
        refresh_token=SECRET_REFRESH,
        client_id="test-client",
        client_secret=SECRET_CLIENT,
        expiry=(datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None),
    )
    original = credentials.to_json()
    settings.token_file.write_text(original)
    refreshed = Credentials(
        SECRET_ACCESS,
        scopes=SCOPES,
        granted_scopes=SCOPES[:1],
        refresh_token=SECRET_REFRESH,
    )
    with (
        patch("app.auth.google.Credentials.from_authorized_user_info", return_value=credentials),
        patch.object(
            credentials,
            "refresh",
            side_effect=lambda request: credentials.__dict__.update(refreshed.__dict__),
        ),
        pytest.raises(AppError, match="Scopes obrigatórios"),
    ):
        authenticate(settings)
    assert settings.token_file.read_text() == original


@pytest.mark.parametrize("arguments", [["auth", "--debug"], ["--debug", "auth"]])
def test_cli_auth_debug_is_safe(tmp_path, arguments):
    settings = configure(tmp_path)
    captured, make_server, browser_open = local_server_mocks()
    with (
        patch("app.cli.commands.load_settings", return_value=settings),
        patch(
            "google_auth_oauthlib.flow.wsgiref.simple_server.make_server", side_effect=make_server
        ),
        patch("google_auth_oauthlib.flow.webbrowser.get") as browser,
        patch.object(
            OAuth2Session, "request", return_value=token_response(" ".join([*SCOPES, EXTRA]))
        ),
    ):
        browser.return_value.open.side_effect = browser_open
        result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert "credentials.valid: True" in result.output
    assert "refresh token presente: True" in result.output
    assert "scopes concedidos" in result.output
    for secret in (SECRET_ACCESS, SECRET_REFRESH, SECRET_CLIENT, "test-code-do-not-log"):
        assert secret not in result.output


def test_unrelated_warning_is_not_accepted(tmp_path):
    settings = configure(tmp_path)
    with patch("app.auth.google.InstalledAppFlow") as flow:
        flow.from_client_config.return_value.run_local_server.side_effect = Warning(SECRET_ACCESS)
        with pytest.raises(AppError, match="Aviso inesperado") as caught:
            authenticate(settings, interactive=True)
    assert not settings.token_file.exists()
    assert SECRET_ACCESS not in str(caught.value)


@pytest.mark.parametrize(
    "granted",
    [
        [SCOPES[0]],
        [SCOPES[2]],
        [SCOPES[0], "https://www.googleapis.com/auth/classroom.coursework.students.readonly"],
        [SCOPES[0], "https://www.googleapis.com/auth/classroom.coursework.me"],
        [
            SCOPES[0],
            "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly.fake",
        ],
    ],
)
def test_alias_does_not_allow_missing_or_unrelated_permissions(granted):
    from app.auth.google import require_scopes

    with pytest.raises(AppError, match="Scopes obrigatórios"):
        require_scopes(granted)


def test_refresh_with_canonical_alias_preserves_actual_grant(tmp_path):
    settings = configure(tmp_path)
    credentials = Credentials(
        SECRET_ACCESS,
        scopes=SCOPES,
        refresh_token=SECRET_REFRESH,
        client_id="test-client",
        client_secret=SECRET_CLIENT,
        expiry=(datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None),
    )
    settings.token_file.write_text(credentials.to_json())
    canonical = [SCOPES[0], SCOPES[2], *SCOPES[3:]]
    refreshed = Credentials(
        SECRET_ACCESS,
        scopes=SCOPES,
        granted_scopes=canonical,
        refresh_token=SECRET_REFRESH,
        client_id="test-client",
        client_secret=SECRET_CLIENT,
        expiry=(datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None),
    )
    with (
        patch("app.auth.google.Credentials.from_authorized_user_info", return_value=credentials),
        patch.object(
            credentials,
            "refresh",
            side_effect=lambda request: credentials.__dict__.update(refreshed.__dict__),
        ),
    ):
        assert authenticate(settings).valid
    payload = json.loads(settings.token_file.read_text())
    assert set(payload["granted_scopes"]) == set(canonical)
    assert SCOPES[1] not in payload["granted_scopes"]
    assert authenticate(settings).valid
