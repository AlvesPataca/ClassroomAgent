class AppError(Exception):
    """Safe, user-facing error. Never include raw provider exceptions or payloads."""


class ReauthenticationRequired(AppError):
    """Required scope evidence is missing from the cached or granted OAuth token."""
