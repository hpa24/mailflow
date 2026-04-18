from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PB_URL: str = "http://pocketbase:8090"
    PB_ADMIN_EMAIL: str
    PB_ADMIN_PASSWORD: str
    PB_DATA_PATH: str = "/app/fts/fts.db"
    ANTHROPIC_API_KEY: str = ""

    model_config = {"env_file": ".env"}


settings = Settings()
