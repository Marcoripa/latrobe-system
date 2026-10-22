import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

import firebase_admin
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import webhooks
from app.api.v1.router import api_v1_router
from app.core.config import settings
from app.core.security import initialize_firebase
from app.services.telegram_service import TelegramNotificationError, telegram_service

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown lifecycle events."""
    initialize_firebase()

    # Automatically register the Telegram webhook on startup when webhook settings are configured
    if (
        settings.TELEGRAM_BOT_TOKEN
        and settings.TELEGRAM_WEBHOOK_BASE_URL
        and settings.TELEGRAM_WEBHOOK_SECRET
    ):
        try:
            await telegram_service.set_webhook(
                base_https_url=settings.TELEGRAM_WEBHOOK_BASE_URL,
                secret_token=settings.TELEGRAM_WEBHOOK_SECRET,
            )
        except TelegramNotificationError as exc:
            logger.warning("Could not register Telegram webhook on startup: %s", exc)

    yield


app = FastAPI(
    title="LaTrobe Irrigation Brain API",
    description=(
        "Asynchronous operations backend connecting Cloud Firestore, "
        "QuickBooks Online, Telegram alerts, and the MCP tool layer."
    ),
    version=settings.VERSION,
    lifespan=lifespan,
)

# Configure CORS for local development and PWA frontend domains
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_origin_regex=r"https://(.*\.latrobeirrigation\.com\.au|latrobe-projectmanager\.(web\.app|firebaseapp\.com))",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register top-level webhook router (`POST /webhook/telegram/{secret_token}`)
app.include_router(webhooks.router)

# Register API v1 router (`/api/v1/shifts`, `/api/v1/projects`, `/api/v1/inventory`, `/api/v1/webhook/telegram/{secret_token}`)
app.include_router(api_v1_router)


@app.get("/health", tags=["System"])
async def health_check() -> dict[str, str | bool]:
    """Return service health and integration readiness status."""
    try:
        firebase_admin.get_app()
        firebase_initialized = True
    except ValueError:
        firebase_initialized = False

    return {
        "status": "healthy",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "firebase_initialized": firebase_initialized,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
