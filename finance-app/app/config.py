"""Environment-driven settings. Read lazily so tests can override env vars."""
import os


def _b(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    @property
    def demo_mode(self) -> bool:
        return _b("DEMO_MODE", "false")

    @property
    def app_password(self) -> str:
        return os.getenv("APP_PASSWORD", "")

    @property
    def session_secret(self) -> str:
        return os.getenv("SESSION_SECRET", "") or "dev-only-insecure-secret"

    @property
    def fernet_key(self) -> str:
        return os.getenv("FERNET_KEY", "")

    @property
    def db_path(self) -> str:
        return os.getenv("DB_PATH", "./data/finance.db")

    @property
    def anthropic_key(self) -> str:
        return os.getenv("ANTHROPIC_API_KEY", "")

    @property
    def model(self) -> str:
        return os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

    @property
    def plaid_client_id(self) -> str:
        return os.getenv("PLAID_CLIENT_ID", "")

    @property
    def plaid_secret(self) -> str:
        return os.getenv("PLAID_SECRET", "")

    @property
    def plaid_env(self) -> str:
        return os.getenv("PLAID_ENV", "sandbox")

    @property
    def plaid_redirect_uri(self) -> str:
        return os.getenv("PLAID_REDIRECT_URI", "")

    @property
    def plaid_configured(self) -> bool:
        return bool(self.plaid_client_id and self.plaid_secret)


settings = Settings()
