"""Cross-center assay measurement-calibration endpoints.

Data admins register platform/assay metadata; researchers and reviewers read
calibration coverage, bias diagnostics and the non-comparable share of samples.
Patient-level lab records are never stored or returned here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..agents import get_agent_client
from ..db import get_db
from ..deps import current_user, require_role
from ..harmonize import fit_assay, sync_assay
from ..models import (
    AssayPlatform,
    CalibrationModel,
    CalibrationSample,
    Center,
    CenterAssay,
    QCBatch,
    Run,
    RunCalibration,
    User,
)
from ..security import content_hash

router = APIRouter(prefix="/api/harmonization", tags=["harmonization"])
admin = require_role("data_admin")


def _center(db: Session, code: str) -> Center:
    c = db.query(Center).filter(Center.code == code).first()
    if not c:
        raise HTTPException(404, "unknown center")
    return c


def _assay(db: Session, center_id: int, variable: str = "followup.lab_value") -> CenterAssay:
    a = (db.query(CenterAssay)
         .filter(CenterAssay.center_id == center_id,
                 CenterAssay.variable == variable).first())
    if not a:
        raise HTTPException(404, "center has no registered assay for this variable")
    return a


def platform_view(p: AssayPlatform) -> dict:
    return {"id": p.id, "code": p.code, "name": p.name, "method": p.method,
            "manufacturer": p.manufacturer, "traceability": p.traceability,
            "reference_material": p.reference_material}


def assay_view(a: CenterAssay, db: Session) -> dict:
    qc = (db.query(QCBatch).filter(QCBatch.assay_id == a.id)
          .order_by(QCBatch.period_start).all())
    latest = (db.query(CalibrationModel)
              .filter(CalibrationModel.assay_id == a.id)
              .order_by(CalibrationModel.version_no.desc()).first())
    samples = db.query(CalibrationSample).filter(CalibrationSample.assay_id == a.id).all()
    return {
        "id": a.id, "center_code": a.center.code, "center_name": a.center.name,
        "variable": a.variable, "unit": a.unit, "platform": platform_view(a.platform),
        "reagent_lot": a.reagent_lot, "calibration_curve": a.calibration_curve,
        "reference_interval": a.reference_interval,
        "measuring_range": a.measuring_range,
        "detection_limit": a.detection_limit,
        "quantitation_limit": a.quantitation_limit,
        "upper_reportable": a.upper_reportable,
        "valid_from": a.valid_from, "valid_to": a.valid_to, "status": a.status,
        "qc_batches": [{"lot": q.lot, "period_start": q.period_start,
                        "period_end": q.period_end, "level": q.level,
                        "observed_cv_pct": q.observed_cv_pct,
                        "allowed_cv_pct": q.allowed_cv_pct,
                        "status": q.status, "note": q.note} for q in qc],
        "sample_counts": {
            "reference": sum(1 for s in samples if s.kind == "reference"),
            "bridge": sum(1 for s in samples if s.kind == "bridge"),
            "holdout": sum(1 for s in samples if s.role == "holdout"),
        },
        "latest_model": model_view(latest) if latest else None,
    }


def model_view(m: CalibrationModel) -> dict:
    t = m.transform or {}
    return {
        "id": m.id, "assay_id": m.assay_id, "center_code": m.assay.center.code,
        "version_no": m.version_no, "route": m.route, "status": m.status,
        "transform": t, "validation": m.validation, "diagnostics": m.diagnostics,
        "fit_params": m.fit_params, "notes": m.notes,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "transform_hash": content_hash(t) if t else None,
    }


@router.get("/platforms")
def list_platforms(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [platform_view(p) for p in db.query(AssayPlatform).order_by(AssayPlatform.code)]


@router.get("/assays")
def list_assays(db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = (db.query(CenterAssay).join(Center, Center.id == CenterAssay.center_id)
            .order_by(Center.code).all())
    return [assay_view(a, db) for a in rows]


@router.get("/assays/{center_code}")
def get_assay(center_code: str, variable: str = "followup.lab_value",
              db: Session = Depends(get_db), _: User = Depends(current_user)):
    center = _center(db, center_code)
    return assay_view(_assay(db, center.id, variable), db)


@router.get("/samples/{center_code}")
def list_samples(center_code: str, variable: str = "followup.lab_value",
                 db: Session = Depends(get_db), _: User = Depends(current_user)):
    """De-identified reference/bridge sample values used to fit the transfer."""
    center = _center(db, center_code)
    a = _assay(db, center.id, variable)
    rows = (db.query(CalibrationSample)
            .filter(CalibrationSample.assay_id == a.id)
            .order_by(CalibrationSample.sample_code).all())
    return [{"sample_code": s.sample_code, "kind": s.kind,
             "reference_value": s.reference_value,
             "measured_value": s.measured_value,
             "partner_value": s.partner_value, "lot": s.lot,
             "role": s.role, "measured_on": s.measured_on} for s in rows]


@router.post("/centers/{center_code}/sync")
def sync_center(center_code: str, db: Session = Depends(get_db),
                _: User = Depends(admin)):
    center = _center(db, center_code)
    try:
        payload = get_agent_client().assay(center_code)
    except Exception as e:
        raise HTTPException(502, f"center agent expose no assay metadata: {e}")
    a = sync_assay(db, center, payload)
    return assay_view(a, db)


class FitIn(BaseModel):
    route: str | None = None
    exclude_lots: list[str] = []


@router.post("/assays/{center_code}/fit")
def fit(center_code: str, body: FitIn, db: Session = Depends(get_db),
        user: User = Depends(admin)):
    center = _center(db, center_code)
    a = _assay(db, center.id)
    m = fit_assay(db, a, user.id, forced_route=body.route,
                  exclude_lots=body.exclude_lots)
    return model_view(m)


@router.get("/models")
def list_models(center_code: str | None = None,
                db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = db.query(CalibrationModel).order_by(CalibrationModel.id)
    if center_code:
        center = _center(db, center_code)
        q = q.join(CenterAssay, CenterAssay.id == CalibrationModel.assay_id) \
             .filter(CenterAssay.center_id == center.id)
    return [model_view(m) for m in q.all()]


@router.get("/models/{model_id}")
def get_model(model_id: int, db: Session = Depends(get_db),
              _: User = Depends(current_user)):
    m = db.get(CalibrationModel, model_id)
    if not m:
        raise HTTPException(404, "unknown model")
    return model_view(m)


@router.get("/coverage")
def coverage(variable: str = "followup.lab_value",
             db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Calibration coverage: which centers are group-comparable, and why not."""
    rows = []
    for center in db.query(Center).order_by(Center.code):
        a = (db.query(CenterAssay)
             .filter(CenterAssay.center_id == center.id,
                     CenterAssay.variable == variable).first())
        if not a:
            rows.append({"center_code": center.code, "status": "no_registration",
                         "comparable": False})
            continue
        m = (db.query(CalibrationModel)
             .filter(CalibrationModel.assay_id == a.id)
             .order_by(CalibrationModel.version_no.desc()).first())
        if not m:
            reason = ("center_has_no_local_lab_field"
                      if a.status == "no_local_field" else "not_fitted")
            rows.append({"center_code": center.code, "status": reason,
                         "platform": a.platform.code, "comparable": False,
                         "reasons": [reason]})
            continue
        diag = m.diagnostics or {}
        rows.append({
            "center_code": center.code, "platform": a.platform.code,
            "traceability": a.platform.traceability,
            "route": m.route, "model_id": m.id, "version_no": m.version_no,
            "status": m.status, "comparable": m.status in ("validated", "active")
            and bool((m.transform or {}).get("enabled")),
            "range_low": (m.transform or {}).get("range_low"),
            "range_high": (m.transform or {}).get("range_high"),
            "n_comparable_ratio_samples": diag.get("n_samples"),
            "reasons": diag.get("reasons", []),
            "flags": (m.transform or {}).get("flags", []),
        })
    comparable = [r["center_code"] for r in rows if r.get("comparable")]
    return {
        "variable": variable,
        "centers": rows,
        "comparable_centers": comparable,
        "coverage_ratio": round(len(comparable) / len(rows), 4) if rows else None,
    }


@router.get("/bias/{center_code}")
def bias(center_code: str, variable: str = "followup.lab_value",
         db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Bias diagnostics: raw vs standardized error and per-lot drift."""
    center = _center(db, center_code)
    a = _assay(db, center.id, variable)
    m = (db.query(CalibrationModel)
         .filter(CalibrationModel.assay_id == a.id)
         .order_by(CalibrationModel.version_no.desc()).first())
    if not m:
        raise HTTPException(404, "assay has no fitted calibration model")
    return {"center_code": center_code, "variable": variable,
            "model_id": m.id, "version_no": m.version_no, "route": m.route,
            "status": m.status, "diagnostics": m.diagnostics,
            "validation": m.validation}


@router.get("/runs/{run_id}")
def run_calibration(run_id: int, db: Session = Depends(get_db),
                    _: User = Depends(current_user)):
    """The exact calibration versions a run used (history is never rewritten)."""
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "unknown run")
    centers = {c.id: c for c in db.query(Center).all()}
    out = []
    for rc in (db.query(RunCalibration)
               .filter(RunCalibration.run_id == run_id)
               .order_by(RunCalibration.center_id)):
        t = rc.transform or {}
        out.append({
            "center_code": centers[rc.center_id].code,
            "variable": rc.variable, "model_id": rc.model_id,
            "model_version_no": rc.model_version_no, "route": rc.route,
            "status": rc.status, "enabled": bool(t.get("enabled")),
            "transform": t,
            "transform_hash": content_hash(t) if t else None,
        })
    return {"run_id": run_id, "calibrations": out}
