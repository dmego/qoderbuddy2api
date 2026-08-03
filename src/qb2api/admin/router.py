"""Admin API router composition."""

from fastapi import APIRouter

from .account_routes import router as account_router
from .catalog_routes import router as catalog_router
from .checkin_routes import router as checkin_router
from .import_routes import router as import_router
from .metric_history_routes import router as metric_history_router
from .observability_routes import router as observability_router
from .proxy_key_routes import router as proxy_key_router
from .security_routes import router as security_router
from .session_routes import router as session_router
from .settings_routes import router as settings_router

router = APIRouter(prefix="/api/admin", tags=["admin"])
router.include_router(session_router)
router.include_router(account_router)
router.include_router(import_router)
router.include_router(checkin_router)
router.include_router(catalog_router)
router.include_router(observability_router)
router.include_router(metric_history_router)
router.include_router(proxy_key_router)
router.include_router(settings_router)
router.include_router(security_router)
