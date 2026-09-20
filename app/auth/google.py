import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from oauthlib.oauth2 import OAuth2Error

from app.config import SCOPES, WRITE_SCOPES, Settings
from app.errors import AppError, ReauthenticationRequired

Diagnostic = Callable[[str], None]

# Google may canonicalize these two names to student-submissions.me.readonly.
# Both describe the student's own coursework/grades; do not generalize to
# *.students, write scopes, courses or arbitrary similarly named permissions.
_STUDENT_READ_ALIASES = frozenset(
    {
        "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
        "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    }
)


def scope_capabilities(scopes: frozenset[str]) -> frozenset[str]:
    return scopes | _STUDENT_READ_ALIASES if scopes & _STUDENT_READ_ALIASES else scopes


def normalize_scopes(value: object) -> frozenset[str]:
    """OAuth scope strings are space-delimited; lists may differ in order/duplicates."""
    if isinstance(value, str):
        return frozenset(value.split())
    if isinstance(value, (list, tuple, set, frozenset)) and all(
        isinstance(item, str) for item in value
    ):
        return frozenset(scope for item in value for scope in item.split())
    raise AppError("Representação de scopes inválida; execute auth novamente.")


def require_scopes(value: object, diagnostic: Diagnostic | None = None) -> frozenset[str]:
    granted = normalize_scopes(value)
    if diagnostic:
        diagnostic("scopes concedidos: " + json.dumps(sorted(granted)))
    covered = scope_capabilities(granted)
    if diagnostic and covered != granted:
        diagnostic(
            "Equivalência readonly do aluno reconhecida: coursework.me / student-submissions.me."
        )
    missing = set(SCOPES) - covered
    if missing:
        raise ReauthenticationRequired(
            "Scopes obrigatórios não concedidos: "
            + ", ".join(sorted(missing))
            + ". Reautenticação necessária: renomeie o arquivo GOOGLE_TOKEN_FILE "
            "(padrão token.json) e execute python main.py auth. Não remova credentials.json."
        )
    return granted


def validate_credentials(
    credentials: Any,
    diagnostic: Diagnostic | None,
    cached_grant: object = None,
) -> frozenset[str]:
    # has_scopes() checks requested metadata, not the actual granted_scopes.
    # An omitted scope in an OAuth response means unchanged scopes (RFC 6749 §5.1).
    actual = credentials.granted_scopes
    if actual is None:
        actual = cached_grant if cached_grant is not None else credentials.scopes
    if diagnostic:
        diagnostic(f"credentials.valid: {bool(credentials.valid)}")
        diagnostic(f"credentials.expired: {bool(credentials.expired)}")
        diagnostic(f"refresh token presente: {bool(credentials.refresh_token)}")
    granted = require_scopes(actual, diagnostic)
    if not credentials.valid or credentials.expired:
        raise AppError("Credenciais OAuth inválidas ou expiradas. Execute auth novamente.")
    if not credentials.refresh_token:
        raise AppError("OAuth sem refresh token. Revogue o acesso do app no Google e execute auth.")
    return granted


def recover_scope_change(
    flow: Any,
    warning: Warning,
    diagnostic: Diagnostic | None,
    expected_scopes: tuple[str, ...] = SCOPES,
    require_write: bool = False,
) -> Any:
    """Recover only OAuthLib's validated scope-change token, never arbitrary warnings.

    OAuthLib 3.x validates state and token errors before raising this Warning;
    it attaches the parsed token but OAuth2Session has not stored it yet.
    No global OAUTHLIB_RELAX_TOKEN_SCOPE or OAuth security checks are disabled.
    """
    token = getattr(warning, "token", None)
    old = getattr(warning, "old_scope", None)
    new = getattr(warning, "new_scope", None)
    if not isinstance(token, dict) or not token.get("access_token") or old is None or new is None:
        raise AppError("Aviso inesperado durante OAuth; nenhum token foi salvo.") from warning
    if normalize_scopes(old) != frozenset(expected_scopes):
        raise AppError("Scopes solicitados inesperados no fluxo OAuth.") from warning
    actual = require_scopes(token.get("scope"), diagnostic)
    if require_write and not set(WRITE_SCOPES).issubset(actual):
        raise ReauthenticationRequired(
            "O Google não concedeu os scopes de escrita; execute auth --write novamente."
        )
    if actual != normalize_scopes(new):
        raise AppError("Scopes inconsistentes na resposta OAuth.") from warning
    # Use the public token setter; the access token/code is never printed or retried.
    flow.oauth2session.token = dict(token)
    return flow.credentials


def save_token(
    credentials: Any, path: Path, *, granted_scopes: frozenset[str] | None = None
) -> None:
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".oauth-", delete=False
        ) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            serialized = credentials.to_json()
            if granted_scopes is not None:
                payload = json.loads(serialized)
                # google-auth to_json() omits granted_scopes; preserve evidence for reload.
                payload["granted_scopes"] = sorted(granted_scopes)
                handle.write(json.dumps(payload))
            else:
                handle.write(serialized)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def authenticate(
    settings: Settings,
    *,
    interactive: bool = False,
    diagnostic: Diagnostic | None = None,
    require_write: bool = False,
) -> Any:
    """Only `auth` opens a browser; read commands refresh cached credentials silently."""
    try:
        if diagnostic:
            diagnostic("scopes requeridos: " + json.dumps(list(SCOPES)))
        requested_scopes = (*SCOPES, *WRITE_SCOPES) if require_write else SCOPES
        if settings.token_file.exists():
            payload = json.loads(settings.token_file.read_text(encoding="utf-8"))
            cached_grant = require_scopes(
                payload.get("granted_scopes", payload.get("scopes")), diagnostic
            )
            if require_write and not set(WRITE_SCOPES).issubset(cached_grant):
                raise ReauthenticationRequired(
                    "Scopes de escrita ausentes (Drive/Classroom); renomeie "
                    "GOOGLE_TOKEN_FILE e execute "
                    "python main.py auth --write."
                )
            # Google ships this factory without a typed signature.
            credentials = Credentials.from_authorized_user_info(payload, scopes=requested_scopes)  # type: ignore[no-untyped-call]
            if credentials.valid:
                validate_credentials(credentials, diagnostic, cached_grant)
                return credentials
            if credentials.refresh_token:
                credentials.refresh(Request())
                granted = validate_credentials(credentials, diagnostic, cached_grant)
                save_token(credentials, settings.token_file, granted_scopes=granted)
                return credentials
            raise AppError("Token sem refresh token. Remova token.json e execute auth.")
        if not interactive:
            raise AppError("Conta não autenticada. Execute python main.py auth.")
        if not settings.credentials_file.is_file():
            raise AppError("credentials.json não encontrado. Baixe credenciais OAuth Desktop.")
        config = json.loads(settings.credentials_file.read_text(encoding="utf-8"))
        if "installed" not in config:
            raise AppError("As credenciais devem ser do tipo OAuth Desktop App.")
        flow = InstalledAppFlow.from_client_config(config, scopes=requested_scopes)
        try:
            credentials = flow.run_local_server(
                host="localhost",
                port=0,
                open_browser=True,
                timeout_seconds=180,
                authorization_prompt_message="Autorize a conta no navegador aberto.",
                success_message="Callback recebido. Confira o resultado no terminal.",
                access_type="offline",
                prompt="consent",
                include_granted_scopes="false",
            )
        except Warning as warning:
            credentials = recover_scope_change(
                flow, warning, diagnostic, requested_scopes, require_write
            )
        granted = validate_credentials(credentials, diagnostic)
        save_token(credentials, settings.token_file, granted_scopes=granted)
        return credentials
    except AppError:
        raise
    except GoogleAuthError as exc:
        raise AppError(
            "Falha no token Google. Confira a conexão; "
            "se revogado, remova token.json e execute auth."
        ) from exc
    except (OAuth2Error, Warning) as exc:
        raise AppError("OAuth recusado ou permissões incompletas. Execute auth novamente.") from exc
    except (ValueError, KeyError, TypeError, OSError, AttributeError) as exc:
        raise AppError(
            "Falha no OAuth ou arquivo local. Confira credenciais, token e permissões."
        ) from exc
