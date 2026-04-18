from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


class SyncStatusResponse(BaseModel):
    total: int
    done: int
    percent: float
    errors: int
    running: bool
    last_sync: str | None = None
