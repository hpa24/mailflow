import asyncio
import logging
import secrets as _secrets
import uuid as _uuid_mod
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from rate_limit import limiter
from routers import admin as admin_router
from routers import ai as ai_router
from routers import bulk as bulk_router
from routers import contacts as contacts_router
from routers import mail as mail_router
from routers import system as system_router
from routers import templates as templates_router
from routers import webhooks as webhooks_router
from services.mail import (
    _bulk_attachments_by_id,
    _bulk_restart_cleanup,
    _bulk_worker_loop,
    _cleanup_temp_uploads_loop,
)

import pb_client
from pb_client import start_token_refresh, stop_token_refresh
import pb_setup
import pb_user_auth
import signed_url
from backfill import backfill_html_once, rebuild_fts_if_needed, run_once_if_needed
from config import settings
from fts import fts_setup
from idle_manager import idle_manager
import spam_filter
from scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Upload-Limits + _email_filters → routers/mail.py


# Send-Pipeline (B15 Bulk-Worker, Restart-Cleanup, _do_send_job etc.):
# → services/mail.py (5c.1). main.py importiert nur die Entry-Points
# (_cleanup_temp_uploads_loop, _bulk_restart_cleanup, _bulk_worker_loop)
# für den lifespan unten.


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Mailflow backend...")

    await pb_client.authenticate()
    start_token_refresh()
    await pb_setup.setup_pocketbase_schema(pb_client.get_token())
    fts_setup(settings.PB_DATA_PATH)
    start_scheduler()
    await idle_manager.start()
    upload_cleanup_task = asyncio.create_task(_cleanup_temp_uploads_loop())

    # B15: vor Worker-Start einmalig has_attachments-Bulks abräumen, dann
    # Worker-Loop für offene queued-Empfänger starten.
    await _bulk_restart_cleanup()
    bulk_worker_task = asyncio.create_task(_bulk_worker_loop(_bulk_attachments_by_id))

    if settings.QDRANT_URL:
        try:
            from vector_store import ensure_collection
            await ensure_collection()
            await spam_filter.ensure_spam_collection()
        except Exception as _e:
            logger.warning("Qdrant nicht erreichbar beim Start — Vector Store deaktiviert: %s", _e)

    for coro in (run_once_if_needed(), rebuild_fts_if_needed(), backfill_html_once()):
        task = asyncio.create_task(coro)
        task.add_done_callback(
            lambda t: t.exception() and logger.error("Background-Task fehlgeschlagen: %s", t.exception())
        )

    logger.info("Mailflow backend ready")
    yield
    upload_cleanup_task.cancel()
    bulk_worker_task.cancel()
    with suppress(asyncio.CancelledError):
        await upload_cleanup_task
    with suppress(asyncio.CancelledError):
        await bulk_worker_task
    await idle_manager.stop()
    stop_scheduler()
    stop_token_refresh()
    logger.info("Shutting down Mailflow backend")


def _parse_cors_origins(raw: str) -> list[str]:
    """Parst kommagetrennte CORS-Origins; fügt immer localhost hinzu."""
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    for local in ("http://localhost", "http://127.0.0.1", "null"):
        if local not in origins:
            origins.append(local)
    return origins


app = FastAPI(title="Mailflow API", lifespan=lifespan)

app.state.limiter = limiter
app.include_router(admin_router.router)
app.include_router(ai_router.router)
app.include_router(bulk_router.router)
app.include_router(contacts_router.router)
app.include_router(mail_router.router)
app.include_router(system_router.router)
app.include_router(templates_router.router)
app.include_router(webhooks_router.router)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate-Limit erreicht — bitte später erneut versuchen"},
        headers={"Access-Control-Allow-Origin": request.headers.get("origin", "*")},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(settings.CORS_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    """Auth-Reihenfolge:
    1. Public/Exempt-Routen (health, OPTIONS, externe Webhook-Sends, X-Import-Key)
    2. PB-User-Token (Authorization: Bearer <pb_token>) — validiert gegen PB
    3. Signierte URL (?token=...) für SSE/Inline/Attachments (keine Header möglich)
    """
    path = request.url.path
    if path == "/health" or request.method == "OPTIONS":
        return await call_next(request)
    # Externer Webhook-Send: eigener API-Key pro Webhook im Endpoint selbst
    if path.startswith("/webhooks/") and path.endswith("/send"):
        return await call_next(request)
    # Kontakt-Import: akzeptiert zusätzlich X-Import-Key (für externe Quellen wie FileMaker)
    if path == "/contacts/import" and settings.IMPORT_API_KEY:
        import_key = request.headers.get("X-Import-Key", "")
        if import_key == settings.IMPORT_API_KEY:
            return await call_next(request)

    # /admin/*: separater ADMIN_API_KEY via X-Admin-Key. PB-Bearer reicht hier NICHT,
    # damit eine Frontend-Token-Kompromittierung nicht auch Admin-Funktionen öffnet.
    if path.startswith("/admin/"):
        if not settings.ADMIN_API_KEY:
            return JSONResponse(
                status_code=503,
                content={"detail": "ADMIN_API_KEY nicht konfiguriert"},
                headers={"Access-Control-Allow-Origin": request.headers.get("origin", "*")},
            )
        admin_key = request.headers.get("X-Admin-Key", "")
        if _secrets.compare_digest(admin_key, settings.ADMIN_API_KEY):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"Access-Control-Allow-Origin": request.headers.get("origin", "*")},
        )

    # PB-User-Token via Authorization-Header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        pb_token = auth_header[7:]
        if await pb_user_auth.validate(pb_token):
            return await call_next(request)

    # Signierte URL für Endpoints ohne Header-Möglichkeit (SSE/Inline/Attachments).
    # S3 (2026-05-23): verify bindet jetzt auch die HTTP-Methode — Tokens
    # können nicht mehr für andere Methoden umgewidmet werden.
    sig_token = request.query_params.get("token") or ""
    if sig_token and signed_url.verify(sig_token, path, request.method):
        return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"detail": "Unauthorized"},
        headers={"Access-Control-Allow-Origin": request.headers.get("origin", "*")},
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """Bekannte HTTPException mit explizitem Status durchreichen — Detail bleibt erhalten."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers={"Access-Control-Allow-Origin": request.headers.get("origin", "*")},
    )


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    """Pydantic-Validierungsfehler als flacher `{"detail": "..."}`-String,
    damit das Frontend-Error-Handling (`new Error(j.detail)`) lesbare
    Meldungen statt `[object Object]` zeigt."""
    msgs = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        msg = err.get("msg", "")
        msgs.append(f"{loc}: {msg}" if loc else msg)
    return JSONResponse(
        status_code=422,
        content={"detail": "; ".join(msgs) or "Ungültige Anfrage"},
        headers={"Access-Control-Allow-Origin": request.headers.get("origin", "*")},
    )


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """Unerwartete Fehler: volle Exception + UUID ins Log, an Client nur ref-Hinweis.
    Verhindert Leaks von Pfaden, PocketBase-Details, Stacktraces."""
    ref = _uuid_mod.uuid4().hex[:12]
    logger.error("Unhandled exception on %s (ref=%s): %s", request.url.path, ref, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Interner Fehler", "ref": ref},
        headers={"Access-Control-Allow-Origin": request.headers.get("origin", "*")},
    )


# /health, /sign, /sync/*, /events, /accounts/*, /smtp-servers, /folders
# → routers/system.py


# /contacts/* + /contact-groups/* → routers/contacts.py



# ==========================================================================
# Mail-Endpoints + private Helpers (/search, /emails/*, /attachments/*,
# /spam-rules/*) → routers/mail.py
# ==========================================================================
# _imap_trash, _imap_set_read, _imap_set_answered, _imap_set_answered_safe
# → services/mail.py


# =========================================================================
# AI-Endpoints (Categories, Triage, Analyse, Suggest, Refine, Patterns)
# → routers/ai.py
# =========================================================================


# /xano/user-info → routers/system.py


# =========================================================================
# Webhooks: ausgelagert nach routers/webhooks.py
# =========================================================================


# =========================================================================
# Templates / Snippets / Variablen: ausgelagert nach routers/templates.py
# (inkl. /templates/render — Helpers _replace_placeholder_refs und
# _find_placeholder_usage leben jetzt dort)
# =========================================================================


# /bulk-sends/* CRUD → routers/bulk.py
# (Bulk-Worker-Loop, Locks, Attachments-Cache bleiben hier — siehe lifespan)


# /contact-groups/* + /contacts/bounced + /contacts/{id}/clear-bounce
# + /contacts/import → routers/contacts.py


# /templates/render → routers/templates.py
