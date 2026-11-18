"""Run endpoints: create (approved rule + locked batches), monitor, compare."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..aggregation import aggregate, count_metrics_for
from ..config import settings
from ..db import SessionLocal, get_db
from ..deps import current_user, require_role
from ..models import (
    Center,
    Run,
    RunBatch,
    RunCalibration,
    RuleVersion,
    User,
)
from ..orchestrator import create_run, execute_run
from ..security import content_hash

router = APIRouter(prefix="/api/runs", tags=["runs"])
researcher = require_role("researcher")


def _center_map(db: Session) -> dict[int, Center]:
    return {c.id: c for c in db.query(Center).all()}


def run_summary(run: Run, db: Session) -> dict:
    rv = run.rule_version
    gaps = [c for c in run.center_results if c.status != "success"]
    return {
        "id": run.id, "status": run.status,
        "rule_version_id": run.rule_version_id,
        "rule_version_no": rv.version_no if rv else None,
        "rule_id": rv.rule_id if rv else None,
        "request_spec": run.request_spec,
        "k_threshold": run.k_threshold,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "centers_total": len(run.center_results),
        "centers_failed": len(gaps),
    }


def calibration_view(run: Run, db: Session) -> list[dict]:
    """Per-center calibration snapshot used by this run (immutable once run)."""
    centers = _center_map(db)
    rows = (db.query(RunCalibration)
            .filter(RunCalibration.run_id == run.id)
            .order_by(RunCalibration.center_id).all())
    out = []
    for rc in rows:
        t = rc.transform or {}
        out.append({
            "center_code": centers[rc.center_id].code,
            "variable": rc.variable,
            "model_id": rc.model_id,
            "model_version_no": rc.model_version_no,
            "route": rc.route,
            "status": rc.status,
            "enabled": bool(t.get("enabled")),
            "slope": t.get("slope"), "intercept": t.get("intercept"),
            "range_low": t.get("range_low"), "range_high": t.get("range_high"),
            "detection_limit": t.get("detection_limit"),
            "qc_failed_windows": t.get("qc_failed_windows", []),
            "flags": t.get("flags", []),
            "transform_hash": content_hash(t) if t else None,
        })
    return out


def run_detail(run: Run, db: Session) -> dict:
    centers = _center_map(db)
    batches = (db.query(RunBatch).filter(RunBatch.run_id == run.id).all())
    center_results = []
    for cr in run.center_results:
        c = centers[cr.center_id]
        batch = next((b for b in batches if b.center_id == cr.center_id), None)
        center_results.append({
            "center_code": c.code, "center_name": c.name,
            "status": cr.status, "error": cr.error,
            "duration_ms": cr.duration_ms,
            "batch_id": batch.batch_id if batch else None,
            "batch_label": batch.batch_label if batch else None,
            "total_n": (cr.aggregates or {}).get("total_n"),
        })
    table = aggregate(
        [{"center": centers[cr.center_id].code, "status": cr.status,
          "error": cr.error, "aggregates": cr.aggregates}
         for cr in run.center_results],
        count_metrics_for(run.request_spec),
    )
    d = run_summary(run, db)
    d.update({
        "conditions": run.rule_version.conditions if run.rule_version else [],
        "conditions_hash": run.rule_version.conditions_hash if run.rule_version else None,
        "request_hash": run.request_hash,
        "batches": [{"center_code": centers[b.center_id].code,
                     "batch_id": b.batch_id, "batch_label": b.batch_label}
                    for b in batches],
        "center_results": center_results,
        "calibrations": calibration_view(run, db),
        "result": table,
    })
    return d


class CreateRunIn(BaseModel):
    rule_version_id: int
    request_spec: dict


@router.post("", status_code=201)
def create(body: CreateRunIn, background: BackgroundTasks,
           db: Session = Depends(get_db), user: User = Depends(researcher)):
    rv = db.get(RuleVersion, body.rule_version_id)
    if not rv:
        raise HTTPException(404, "unknown rule version")
    run = create_run(db, rv, body.request_spec, content_hash(body.request_spec),
                     user.id, settings.small_cell_k)
    if settings.run_execution == "sync":
        execute_run(db, run)
    else:
        run_id = run.id

        def _bg():
            with SessionLocal() as s:
                execute_run(s, s.get(Run, run_id))

        background.add_task(_bg)
    return run_detail(run, db)


@router.get("")
def list_runs(db: Session = Depends(get_db), _: User = Depends(current_user)):
    runs = db.query(Run).order_by(Run.id.desc()).all()
    return [run_summary(r, db) for r in runs]


@router.get("/compare")
def compare(a: int, b: int, db: Session = Depends(get_db),
            _: User = Depends(current_user)):
    ra, rb = db.get(Run, a), db.get(Run, b)
    if not ra or not rb:
        raise HTTPException(404, "unknown run")
    return {"a": run_detail(ra, db), "b": run_detail(rb, db)}


@router.get("/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "unknown run")
    return run_detail(run, db)
