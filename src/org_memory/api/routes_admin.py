"""Admin and ops API under /v1/admin.

Mounts focused routers: health/connectors, jobs, and compliance
(spend, legal holds, retention). URL paths stay unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from org_memory.api.deps import require_api_key
from org_memory.api.routes_admin_compliance import router as compliance_router
from org_memory.api.routes_admin_health import router as health_router
from org_memory.api.routes_admin_jobs import router as jobs_router

router = APIRouter(prefix="/v1/admin", dependencies=[Depends(require_api_key)])
router.include_router(health_router)
router.include_router(jobs_router)
router.include_router(compliance_router)
