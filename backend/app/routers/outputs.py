"""Output tables and the reviewer queue: small-cell analysis, decisions, publish."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..aggregation import aggregate, count_metrics_for
from ..db import get_db
from ..deps import current_user, require_role
from ..models import (
    Center,
    OutputTable,
    PublishedReport,
    ReviewDecision,
    Run,
    RunBatch,
    RunCalibration,
    User,
)
from ..privacy import analyze_table, apply_merge, apply_suppression
from ..security import content_hash

router = APIRouter(prefix="/api", tags=["outputs"])
researcher = require_role("researcher")
reviewer = require_role("reviewer")


def output_view(o: OutputTable, db: Session, full: bool = False) -> dict:
    creator = db.get(User, o.created_by)
    out = {
        "id": o.id, "run_id": o.run_id, "title": o.title, "status": o.status,
        "created_by": creator.display_name if creator else o.created_by,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "k_threshold": o.k_threshold,
        "flag_count": len(o.flags),
    }
    if full:
        out["table_data"] = o.table_data
        out["flags"] = o.flags
        out["decisions"] = [
            {"id": d.id, "action": d.action, "detail": d.detail,
             "comment": d.comment,
             "reviewer": db.get(User, d.reviewer_id).display_name,
             "decided_at": d.decided_at.isoformat()}
            for d in db.query(ReviewDecision).filter(ReviewDecision.output_id == o.id)
            .order_by(ReviewDecision.id)
        ]
    return out


def _run_table(run: Run, db: Session) -> dict:
    centers = {c.id: c for c in db.query(Center).all()}
    return aggregate(
        [{"center": centers[cr.center_id].code, "status": cr.status,
          "error": cr.error, "aggregates": cr.aggregates}
         for cr in run.center_results],
        count_metrics_for(run.request_spec),
    )


class CreateOutputIn(BaseModel):
    title: str


@router.post("/runs/{run_id}/outputs", status_code=201)
def create_output(run_id: int, body: CreateOutputIn, db: Session = Depends(get_db),
                  user: User = Depends(researcher)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "unknown run")
    if run.status not in ("completed", "completed_with_gaps"):
        raise HTTPException(409, f"run is {run.status}; outputs require a finished run")
    table = _run_table(run, db)
    flags = analyze_table(table, run.k_threshold)
    o = OutputTable(run_id=run.id, title=body.title, created_by=user.id,
                    table_data=table, flags=flags, k_threshold=run.k_threshold)
    db.add(o)
    db.commit()
    db.refresh(o)
    return output_view(o, db, full=True)


@router.get("/outputs")
def list_outputs(db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = db.query(OutputTable).order_by(OutputTable.id.desc())
    if user.role == "researcher":
        q = q.filter(OutputTable.created_by == user.id)
    return [output_view(o, db) for o in q.all()]


@router.get("/outputs/{output_id}")
def get_output(output_id: int, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    o = db.get(OutputTable, output_id)
    if not o:
        raise HTTPException(404, "unknown output")
    if user.role == "researcher" and o.created_by != user.id:
        raise HTTPException(403, "not your output")
    return output_view(o, db, full=True)


class DecisionIn(BaseModel):
    action: str  # approve | suppress | merge | reject
    detail: dict = {}
    comment: str | None = None


def _publish(db: Session, o: OutputTable, decision: ReviewDecision,
             reviewer: User) -> PublishedReport:
    run = db.get(Run, o.run_id)
    centers = {c.id: c for c in db.query(Center).all()}
    batches = db.query(RunBatch).filter(RunBatch.run_id == run.id).all()
    calibrations = []
    for rc in (db.query(RunCalibration)
               .filter(RunCalibration.run_id == run.id)
               .order_by(RunCalibration.center_id)):
        t = rc.transform or {}
        calibrations.append({
            "center_code": centers[rc.center_id].code,
            "variable": rc.variable,
            "model_id": rc.model_id,
            "model_version_no": rc.model_version_no,
            "route": rc.route,
            "status": rc.status,
            "transform_hash": content_hash(t) if t else None,
            "range_low": t.get("range_low"), "range_high": t.get("range_high"),
            "detection_limit": t.get("detection_limit"),
        })
    report = {
        "title": o.title,
        "table": o.table_data,
        "provenance": {
            "run_id": run.id,
            "request_spec": run.request_spec,
            "request_hash": run.request_hash,
            "rule_version_id": run.rule_version_id,
            "rule_version_no": run.rule_version.version_no,
            "conditions": run.rule_version.conditions,
            "conditions_hash": run.rule_version.conditions_hash,
            "batches": [{"center_code": centers[b.center_id].code,
                         "batch_id": b.batch_id, "batch_label": b.batch_label}
                        for b in batches],
            "k_threshold": run.k_threshold,
            "coverage": o.table_data.get("coverage"),
            "calibrations": calibrations,
            "harmonization": o.table_data.get("harmonization"),
            "review": {"decision_id": decision.id,
                       "reviewer": reviewer.display_name,
                       "action": decision.action,
                       "decided_at": decision.decided_at.isoformat(),
                       "comment": decision.comment},
        },
    }
    pub = PublishedReport(
        output_id=o.id, run_id=run.id, rule_version_id=run.rule_version_id,
        decision_id=decision.id, published_by=reviewer.id,
        report=report, report_hash=content_hash(report),
    )
    db.add(pub)
    return pub


@router.post("/outputs/{output_id}/decision")
def decide(output_id: int, body: DecisionIn, db: Session = Depends(get_db),
           user: User = Depends(reviewer)):
    o = db.get(OutputTable, output_id)
    if not o:
        raise HTTPException(404, "unknown output")
    if o.status != "pending_review":
        raise HTTPException(409, f"output is {o.status}; review already finished")

    action = body.action
    if action == "reject":
        o.status = "rejected"
        d = ReviewDecision(output_id=o.id, reviewer_id=user.id, action="reject",
                           detail=body.detail, comment=body.comment)
        db.add(d)
        db.commit()
        return output_view(o, db, full=True)

    if action == "suppress":
        cells = body.detail.get("cells", [])
        if not cells:
            raise HTTPException(400, "suppress decision needs detail.cells")
        o.table_data = apply_suppression(o.table_data, cells)
    elif action == "merge":
        groups = body.detail.get("groups", [])
        into = body.detail.get("into", "合并组")
        try:
            o.table_data = apply_merge(o.table_data, groups, into)
        except ValueError as e:
            raise HTTPException(400, str(e))
    elif action != "approve":
        raise HTTPException(400, f"unknown action {action}")

    # Re-analyse after modification; approve only when releasable.
    o.flags = analyze_table(o.table_data, o.k_threshold)
    d = ReviewDecision(output_id=o.id, reviewer_id=user.id, action=action,
                       detail=body.detail, comment=body.comment)
    db.add(d)
    db.flush()
    if o.flags:
        if action == "approve":
            db.rollback()
            raise HTTPException(
                409, "output still has unresolved small-cell/differencing flags")
        db.commit()  # intermediate modification, stays in the queue
        return output_view(o, db, full=True)

    o.status = "approved"
    pub = _publish(db, o, d, user)
    db.commit()
    out = output_view(o, db, full=True)
    out["published_report_id"] = pub.id
    return out
