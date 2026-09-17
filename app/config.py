import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr, ValidationError

from app.errors import AppError

ROOT = Path(__file__).resolve().parents[1]
SCOPES = (
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)


class Settings(BaseModel):
    context_max_chars: int = Field(default=60000, ge=100, le=500000)
    llm_provider: str = "mock"
    astra_api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    astra_base_url: str = "https://api.openai.com/v1"
    astra_model: str = "gpt-6-astra"
    attachments_root: Path = ROOT / "data" / "attachments"
    attachment_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    extraction_max_bytes: int = Field(default=40 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    extraction_max_chars: int = Field(default=1_000_000, ge=1, le=5_000_000)
    extraction_timeout_seconds: int = Field(default=30, ge=1, le=120)
    database_file: Path = ROOT / "data" / "classroom.db"
    timezone: str = "America/Sao_Paulo"
    credentials_file: Path = ROOT / "credentials.json"
    token_file: Path = ROOT / "token.json"
    http_timeout_seconds: int = Field(default=30, ge=1, le=300)
    max_retries: int = Field(default=3, ge=0, le=8)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_settings(root: Path = ROOT) -> Settings:
    values = {**dotenv_values(root / ".env"), **os.environ}

    def path(key: str, default: str) -> Path:
        value = Path(values.get(key) or default).expanduser()
        return value if value.is_absolute() else root / value

    try:
        settings = Settings(
            context_max_chars=int(values.get("CONTEXT_MAX_CHARS") or "60000"),
            llm_provider=values.get("LLM_PROVIDER") or "mock",
            astra_api_key=SecretStr(values.get("ASTRA_API_KEY") or ""),
            astra_base_url=values.get("ASTRA_BASE_URL") or "https://api.openai.com/v1",
            astra_model=values.get("ASTRA_MODEL") or "gpt-6-astra",
            attachments_root=root / "data" / "attachments",
            attachment_max_bytes=int(values.get("ATTACHMENT_MAX_BYTES") or "20971520"),
            extraction_max_bytes=int(values.get("EXTRACTION_MAX_BYTES") or "41943040"),
            extraction_max_chars=int(values.get("EXTRACTION_MAX_CHARS") or "1000000"),
            extraction_timeout_seconds=int(values.get("EXTRACTION_TIMEOUT_SECONDS") or "30"),
            database_file=path("DATABASE_FILE", "data/classroom.db"),
            timezone=values.get("TIMEZONE") or "America/Sao_Paulo",
            credentials_file=path("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
            token_file=path("GOOGLE_TOKEN_FILE", "token.json"),
            http_timeout_seconds=int(values.get("HTTP_TIMEOUT_SECONDS") or "30"),
            max_retries=int(values.get("MAX_RETRIES") or "3"),
        )
        _ = settings.zone
        if settings.credentials_file.resolve() == settings.token_file.resolve():
            raise AppError("Credenciais e token devem usar arquivos diferentes.")
        return settings
    except (ValueError, ValidationError, ZoneInfoNotFoundError) as exc:
        raise AppError(
            "Configuração inválida: confira timezone, timeouts, retries "
            "e limites de anexos no .env."
        ) from exc
