import logging
from fastapi import FastAPI
from app.api.health import router as health_router
from app.api.webhooks import router as webhook_router
from app.api.oauth import router as oauth_router
from app.api.slack import router as slack_router
from app.config import get_settings

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = FastAPI(title="Neuron Coding Agent")
app.include_router(health_router)
app.include_router(webhook_router)
app.include_router(oauth_router)
app.include_router(slack_router)
