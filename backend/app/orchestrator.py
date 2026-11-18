"""Run orchestration: snapshot locked batches, dispatch per-center jobs, record
per-center outcomes. A run always references the batches locked at start time;
batch replacement during a run only affects later runs."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .agents import get_agent_client
from .models import (
    CalibrationModel,
    Center,
    CenterAssay,
    CenterMapping,
    DataBatch,
    QCBatch,
    Run,
    RunBatch,
    RunCalibration,
    RunCenterResult,
    RuleVersion,
)


def locked_batches(db: Session) -> dict[int, DataBatch]:
    """center_id -> currently locked batch."""
    rows = db.query(DataBatch).filter(DataBatch.status == "locked").all()
    return {b.center_id: b for b in rows}


def standardized_variables(request_spec: dict) -> list[str]:
    """Lab variables that a request asks to harmonize across centers."""
    out = []
    for m in request_spec.get("metrics", []):
        if m.get("type") == "standardized_mean" and m.get("variable") not in out:
            out.append(m["variable"])
    return out


def active_models(db: Session) -> dict[tuple[int, str], CalibrationModel]:
    """(center_id, variable) -> highest-version model for that assay.

    The most recent version is authoritative even if it was rejected: a failed
    re-validation must *stop* comparability rather than fall back to an older
    transform. Runs snapshot whatever this resolves to at start time.
    """
    rows = (db.query(CalibrationModel)
            .order_by(CalibrationModel.assay_id, CalibrationModel.version_no).all())
    assays = {a.id: a for a in db.query(CenterAssay).all()}
    out: dict[tuple[int, str], CalibrationModel] = {}
    for m in rows:
        a = assays.get(m.assay_id)
        if not a:
            continue
        out[(a.center_id, a.variable)] = m  # later versions overwrite
    return out


def _qc_failed_windows(db: Session, assay_id: int) -> list[dict]:
    windows = []
    for q in (db.query(QCBatch)
              .filter(QCBatch.assay_id == assay_id, QCBatch.status == "failed")):
        windows.append({"lot": q.lot, "start": q.period_start, "end": q.period_end})
    return windows


def _transform_snapshot(db: Session, model: CalibrationModel,
                        assay: CenterAssay) -> dict:
    """Fully self-contained transform sent to the center (nothing to re-derive)."""
    t = dict(model.transform or {})
    t["model_id"] = model.id
    t["model_version_no"] = model.version_no
    t["assay_id"] = assay.id
    t["route"] = model.route
    t["unit"] = assay.unit
    t["valid_from"] = assay.valid_from
    t["valid_to"] = assay.valid_to
    # The center classifies by *date*, so hand it every non-comparable window:
    # QC-failed lots from the QC registry plus analyst-excluded drifted lots
    # recorded on the model version itself.
    windows = _qc_failed_windows(db, assay.id)
    for w in t.get("excluded_lot_windows", []) or []:
        if w not in windows:
            windows.append(w)
    t["qc_failed_windows"] = windows
    return t


def snapshot_calibrations(db: Session, run: Run) -> dict[int, dict]:
    """Freeze each center's calibration version for this run.

    Returns center_id -> {variable: transform}. Also persists a RunCalibration
    row per center so provenance is reproducible after a refit. A new
    calibration therefore only affects new runs.
    """
    variables = standardized_variables(run.request_spec)
    models = active_models(db)
    assays = {(a.center_id, a.variable): a for a in db.query(CenterAssay).all()}
    out: dict[int, dict] = {}
    for cr in run.center_results:
        calib: dict[str, dict] = {}
        for var in variables:
            assay = assays.get((cr.center_id, var))
            model = models.get((cr.center_id, var))
            if model is not None and assay is not None:
                status = ("applied" if model.status in ("validated", "active")
                          else "rejected")
                calib[var] = _transform_snapshot(db, model, assay)
                db.add(RunCalibration(
                    run_id=run.id, center_id=cr.center_id, variable=var,
                    model_id=model.id, model_version_no=model.version_no,
                    route=model.route, transform=calib[var], status=status,
                ))
            else:
                db.add(RunCalibration(
                    run_id=run.id, center_id=cr.center_id, variable=var,
                    model_id=None, model_version_no=None, route=None,
                    transform={}, status="unavailable",
                ))
        out[cr.center_id] = calib
    db.commit()
    return out


def create_run(db: Session, rule_version: RuleVersion, request_spec: dict,
               request_hash: str, user_id: int, k_threshold: int) -> Run:
    if rule_version.status != "approved":
        raise HTTPException(409, "rule version is not approved; runs require an approved rule")
    locked = locked_batches(db)
    if not locked:
        raise HTTPException(409, "no locked data batches; ask a data admin to lock batches first")

    run = Run(rule_version_id=rule_version.id, request_spec=request_spec,
              request_hash=request_hash, status="pending",
              k_threshold=k_threshold, created_by=user_id)
    db.add(run)
    db.flush()
    for center_id, batch in locked.items():
        db.add(RunBatch(run_id=run.id, center_id=center_id, batch_id=batch.id,
                        batch_label=batch.label))
        db.add(RunCenterResult(run_id=run.id, center_id=center_id, status="pending"))
    db.commit()
    db.refresh(run)
    snapshot_calibrations(db, run)
    return run


def execute_run(db: Session, run: Run) -> Run:
    """Dispatch the job to every center in the run's batch snapshot."""
    client = get_agent_client()
    run.status = "running"
    db.commit()

    centers = {c.id: c for c in db.query(Center).all()}
    mappings = _mappings_by_center(db)
    calibrations = _calibration_snapshots(db, run)
    job_base = {
        "job_id": f"run-{run.id}",
        "conditions": run.rule_version.conditions,
        "request_spec": run.request_spec,
    }

    for cr in run.center_results:
        center = centers[cr.center_id]
        job = dict(job_base)
        job["job_id"] = f"run-{run.id}-{center.code}"
        job["mapping"] = mappings.get(cr.center_id, {})
        job["calibrations"] = calibrations.get(cr.center_id, {})
        started = time.monotonic()
        result = client.execute(center.code, job)
        elapsed = int((time.monotonic() - started) * 1000)
        cr.duration_ms = elapsed
        status = result.get("status", "failed")
        if status == "success":
            cr.status = "success"
            cr.aggregates = {"groups": result.get("groups", []),
                             "total_n": result.get("total_n", 0),
                             "harmonization": result.get("harmonization", {})}
            cr.error = None
        elif status == "missing_mapping":
            cr.status = "missing_mapping"
            cr.error = result.get("error", "missing variable mapping")
        else:
            cr.status = "failed"
            cr.error = result.get("error", "unknown failure")
        db.commit()

    run.finished_at = datetime.now(timezone.utc)
    run.status = ("completed" if all(c.status == "success" for c in run.center_results)
                  else "completed_with_gaps")
    db.commit()
    db.refresh(run)
    return run


def _calibration_snapshots(db: Session, run: Run) -> dict[int, dict]:
    """Re-read the frozen RunCalibration rows (never the live model tables)."""
    out: dict[int, dict] = {}
    for rc in db.query(RunCalibration).filter(RunCalibration.run_id == run.id):
        if rc.status == "applied" and rc.transform:
            out.setdefault(rc.center_id, {})[rc.variable] = rc.transform
        else:
            out.setdefault(rc.center_id, {})
    return out


def _mappings_by_center(db: Session) -> dict[int, dict]:
    """center_id -> {variable: {field, value_map}} for the active dictionary."""
    rows = db.query(CenterMapping).all()
    out: dict[int, dict] = {}
    for m in rows:
        if m.status != "mapped" or not m.local_field:
            continue
        out.setdefault(m.center_id, {})[m.variable_name] = {
            "field": m.local_field,
            "value_map": m.value_map,
        }
    return out
