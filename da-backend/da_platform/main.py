from __future__ import annotations

import logging

from fastapi import FastAPI

from da_platform.api import auth, documents, health, sections, silos, uploads
from da_platform.db.session import create_all
from da_platform.settings import settings
from da_platform.silo_registry import discover_silos

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(
        title="ARIA — AI-Driven Report Intelligence & Automation", version="0.1.0"
    )

    # Every route is mounted under /api, matching the ingress contract in spec
    # section 3 (/api/* -> api:8000). The Vite dev proxy reproduces that locally,
    # so the session cookie is same-origin in development too and no CORS
    # exception is needed.
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(silos.router, prefix="/api")
    app.include_router(uploads.router, prefix="/api")
    app.include_router(documents.router, prefix="/api")
    app.include_router(sections.router, prefix="/api")

    # Silo-specific endpoints, mounted under the silo's own path. Additive: a new
    # silo with a router.py appears here without this file changing.
    for silo in discover_silos():
        if silo.router is not None:
            app.include_router(silo.router, prefix=f"/api/silos/{silo.id}")
            logger.info("Mounted the %s silo router", silo.id)

    if settings.is_local:
        # Locally this replaces running migrations by hand. Alembic owns schema
        # changes from the dev environment onward (D23).
        create_all()

    return app


app = create_app()
