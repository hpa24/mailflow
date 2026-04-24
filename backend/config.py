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
    # Wenn gesetzt, müssen alle API-Anfragen diesen Key mitsenden.
    # Leer lassen für lokale Entwicklung ohne Auth.
    API_KEY: str = ""
    # Kommagetrennte Liste erlaubter CORS-Origins, z.B. "https://mailflow.barres.de"
    CORS_ORIGINS: str = "https://mailflow.barres.de"
    XANO_API_KEY: str = "rnj!wkj7nzj_ezw8QZW"
    XANO_USER_ROLES_URL: str = "https://xdmv-h2vh-soia.f2.xano.io/api:52vvrgF7/user/get/roles"

    model_config = {"env_file": ".env"}


settings = Settings()
