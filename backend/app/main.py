"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import Base, SessionLocal, engine
from .routers import admin, auth, harmonization, meta, outputs, reports, rules, runs
from .seed import seed

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("platform")

app = FastAPI(title="罕见病多中心受控分析平台", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(meta.router)
app.include_router(harmonization.router)
app.include_router(admin.router)
app.include_router(rules.router)
app.include_router(runs.router)
app.include_router(outputs.router)
app.include_router(reports.router)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(engine)
    if settings.seed_on_startup:
        with SessionLocal() as db:
            try:
                seed(db)
                log.info("seed complete")
            except Exception:
                log.exception("seed failed (agents not ready?)")


@app.get("/api/health")
def health():
    return {"status": "ok"}


# Optional: serve the built frontend when present (single-container demo).
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
