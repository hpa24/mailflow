from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PB_URL: str = "http://pocketbase:8090"
    PB_ADMIN_EMAIL: str
    PB_ADMIN_PASSWORD: str
    PB_DATA_PATH: str = "/app/fts/fts.db"
    ANTHROPIC_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    QDRANT_URL: str = ""
    QDRANT_API_KEY: str = ""
    SPAM_SIMILARITY_THRESHOLD: float = 0.82
    SPAM_AUTO_CLASSIFY: bool = False
    # Optionaler separater Key für externen Kontakt-Import (FileMaker, Xano etc.).
    # Wird per X-Import-Key-Header geprüft. Leer = Endpoint nur per PB-Login erreichbar.
    IMPORT_API_KEY: str = ""
    # Separater Schlüssel für /admin/*-Endpoints (Embed-Backfill, IMAP-UID-Backfill, Embed-Suche).
    # Wird per X-Admin-Key-Header geprüft. Leer = /admin/* liefert 503.
    ADMIN_API_KEY: str = ""
    # HMAC-Secret für kurzlebige signierte URLs (SSE, Attachments, Inline-Bilder).
    # Im Coolify zufällig generieren — bei Rotation invalidieren alte Links sofort.
    SIGN_SECRET: str = ""
    # Kommagetrennte Liste erlaubter CORS-Origins, z.B. "https://mailflow.barres.de"
    CORS_ORIGINS: str = "https://mailflow.barres.de"
    XANO_API_KEY: str = ""
    XANO_USER_ROLES_URL: str = "https://xdmv-h2vh-soia.f2.xano.io/api:52vvrgF7/user/get/roles"
    # Zweite PocketBase: der Activity-Kalender-Store (separate Instanz, nicht die
    # mailflow-eigene PB). Schreibt Kalender-Einladungen als termine-Records.
    # Auth als normaler User (stefan@hpa24.de, kein Superuser). Feature ist
    # deaktiviert, solange IDENTITY/PASSWORD leer sind.
    ACTIVITY_PB_URL: str = "https://activity-pb.barres.de"
    ACTIVITY_PB_IDENTITY: str = ""
    ACTIVITY_PB_PASSWORD: str = ""

    model_config = {"env_file": ".env"}


settings = Settings()
