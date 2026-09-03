from fastapi import APIRouter
from app.config import get_settings

router = APIRouter()
@router.get("/health")
async def health(): return {"status":"ok"}
@router.get("/ready")
async def ready():
    checks = get_settings().readiness()
    return {"status":"ready", **checks}
