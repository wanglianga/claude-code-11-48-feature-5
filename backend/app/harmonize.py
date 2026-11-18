"""Register assay metadata from centers and fit versioned calibration models."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .calibration import build_model
from .models import (
    AssayPlatform,
    CalibrationModel,
    CalibrationSample,
    Center,
    CenterAssay,
    QCBatch,
)


def _platform_from_payload(db: Session, payload: dict) -> AssayPlatform:
    p = payload.get("platform") or {}
    code = p.get("code")
    if not code:
        raise HTTPException(400, "assay payload missing platform.code")
    platform = db.query(AssayPlatform).filter(AssayPlatform.code == code).first()
    if platform is None:
        platform = AssayPlatform(code=code)
        db.add(platform)
    platform.name = p.get("name", code)
    platform.manufacturer = p.get("manufacturer")
    platform.method = p.get("method", "unknown")
    platform.traceability = p.get("traceability", "bridged")
    platform.reference_material = p.get("reference_material")
    db.flush()
    return platform


def sync_assay(db: Session, center: Center, payload: dict) -> CenterAssay:
    """Upsert platform catalog, center registration, QC windows, samples."""
    platform = _platform_from_payload(db, payload)
    spec = payload.get("assay") or {}
    variable = payload.get("variable", "followup.lab_value")
    assay = (db.query(CenterAssay)
             .filter(CenterAssay.center_id == center.id,
                     CenterAssay.variable == variable).first())
    if assay is None:
        assay = CenterAssay(center_id=center.id, variable=variable,
                            platform_id=platform.id)
        db.add(assay)
    assay.platform_id = platform.id
    assay.unit = payload.get("unit", spec.get("unit", "mg/L"))
    assay.reagent_lot = spec.get("reagent_lot", "unregistered")
    assay.calibration_curve = spec.get("calibration_curve") or {}
    assay.reference_interval = spec.get("reference_interval") or {}
    assay.measuring_range = spec.get("measuring_range") or {}
    assay.detection_limit = float(spec.get("detection_limit") or 0.0)
    assay.quantitation_limit = spec.get("quantitation_limit")
    assay.upper_reportable = spec.get("upper_reportable")
    assay.valid_from = spec.get("valid_from") or "2026-01-01"
    assay.valid_to = spec.get("valid_to")
    # A center that reports no local lab field is registered but flagged: its
    # metric cannot be harmonized because there is nothing to calibrate.
    assay.status = "registered" if payload.get("has_lab_field", True) \
        else "no_local_field"
    db.flush()

    # QC windows are replaced wholesale on re-sync (they are metadata versions).
    db.query(QCBatch).filter(QCBatch.assay_id == assay.id).delete()
    for q in payload.get("qc_batches", []):
        db.add(QCBatch(
            assay_id=assay.id, lot=q["lot"],
            period_start=q.get("period_start") or assay.valid_from,
            period_end=q.get("period_end"), level=q.get("level", "normal"),
            observed_cv_pct=q.get("observed_cv_pct"),
            allowed_cv_pct=q.get("allowed_cv_pct", 10.0),
            status=q.get("status", "passed"), note=q.get("note"),
        ))

    db.query(CalibrationSample).filter(CalibrationSample.assay_id == assay.id).delete()
    partner_codes: dict[str, int] = {}
    for s in payload.get("samples", []):
        partner_id = None
        pc = s.get("partner_center")
        if pc:
            if pc not in partner_codes:
                partner = db.query(Center).filter(Center.code == pc).first()
                partner_codes[pc] = partner.id if partner else None
            partner_id = partner_codes[pc]
        db.add(CalibrationSample(
            assay_id=assay.id, sample_code=s["sample_code"], kind=s["kind"],
            reference_value=s.get("reference_value"),
            measured_value=s["measured_value"], lot=s.get("lot"),
            partner_center_id=partner_id, partner_value=s.get("partner_value"),
            measured_on=s.get("measured_on"), role=s.get("role", "train"),
        ))
    db.commit()
    db.refresh(assay)
    return assay


def _partner_standardizer(db: Session, assay: CenterAssay,
                          samples: list[CalibrationSample]) -> tuple[dict | None, str | None]:
    """For a bridged assay: the partner's latest validated transform, if any."""
    partner_id = next((s.partner_center_id for s in samples
                       if s.partner_center_id), None)
    if not partner_id:
        return None, None
    partner_assay = (db.query(CenterAssay)
                     .filter(CenterAssay.center_id == partner_id,
                             CenterAssay.variable == assay.variable).first())
    if not partner_assay:
        return None, db.get(Center, partner_id).code
    model = (db.query(CalibrationModel)
             .filter(CalibrationModel.assay_id == partner_assay.id)
             .order_by(CalibrationModel.version_no.desc()).first())
    code = db.get(Center, partner_id).code
    if model and model.status in ("validated", "active") and model.transform.get("enabled"):
        t = dict(model.transform)
        t["valid_from"] = partner_assay.valid_from
        t["valid_to"] = partner_assay.valid_to
        return t, code
    return None, code


def fit_assay(db: Session, assay: CenterAssay, user_id: int,
              forced_route: str | None = None,
              exclude_lots: list[str] | None = None) -> CalibrationModel:
    """Fit + hold-out validate, persist as a new version, supersede the old one."""
    samples = (db.query(CalibrationSample)
               .filter(CalibrationSample.assay_id == assay.id).all())
    qc = db.query(QCBatch).filter(QCBatch.assay_id == assay.id).all()
    partner_std, partner_code = _partner_standardizer(db, assay, samples)
    result = build_model(assay, assay.platform, samples, qc,
                         partner_standardizer=partner_std,
                         forced_route=forced_route, partner_code=partner_code,
                         exclude_lots=exclude_lots)

    prev = (db.query(CalibrationModel)
            .filter(CalibrationModel.assay_id == assay.id)
            .order_by(CalibrationModel.version_no.desc()).all())
    for old in prev:
        if old.status in ("validated", "active"):
            old.status = "superseded"
    next_no = (prev[0].version_no + 1) if prev else 1
    model = CalibrationModel(
        assay_id=assay.id, version_no=next_no, route=result["route"],
        status=result["status"], transform=result["transform"],
        validation=result["validation"], diagnostics=result["diagnostics"],
        fit_params=result["fit_params"], notes=result["notes"],
        created_by=user_id,
    )
    # Excluded lots (QC failure or confirmed drift) become non-comparable
    # periods at run time; they are skipped, never imputed.
    exclude_set = set(exclude_lots or []) | set(
        (result["diagnostics"] or {}).get("failed_lots") or [])
    if exclude_set:
        windows, seen = [], set()
        for q in qc:
            if q.lot in exclude_set and q.lot not in seen:
                seen.add(q.lot)
                windows.append({"lot": q.lot, "start": q.period_start,
                                "end": q.period_end, "reason": "excluded_lot"})
        # store under a distinct key; orchestrator merges with QC-failed windows
        model.transform["excluded_lot_windows"] = windows
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


def sync_all(db: Session, client, user_id: int) -> dict:
    """Pull every center's assay metadata and fit initial models (idempotent)."""
    out = {"synced": [], "failed": []}
    for center in db.query(Center).order_by(Center.code):
        try:
            payload = client.assay(center.code)
        except Exception as e:  # center not exposing assay metadata
            out["failed"].append({"center": center.code, "error": str(e)})
            continue
        sync_assay(db, center, payload)
        out["synced"].append(center.code)
    return out
