"""Data-admin endpoints: centers, dictionary, mappings, data batches."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..agents import get_agent_client
from ..db import get_db
from ..deps import require_role
from ..models import (
    Center,
    CenterMapping,
    DataBatch,
    Dictionary,
    User,
    Variable,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])
admin = require_role("data_admin")


def batch_view(b: DataBatch) -> dict:
    return {
        "id": b.id, "center_id": b.center_id, "center_code": b.center.code,
        "center_name": b.center.name, "label": b.label, "status": b.status,
        "dict_id": b.dict_id, "record_counts": b.record_counts,
        "available_scope": b.available_scope,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "locked_at": b.locked_at.isoformat() if b.locked_at else None,
    }


@router.get("/centers")
def list_centers(db: Session = Depends(get_db), _: User = Depends(admin)):
    out = []
    for c in db.query(Center).order_by(Center.code):
        locked = (db.query(DataBatch)
                  .filter(DataBatch.center_id == c.id, DataBatch.status == "locked")
                  .first())
        out.append({"id": c.id, "code": c.code, "name": c.name,
                    "agent_url": c.agent_url,
                    "locked_batch": batch_view(locked) if locked else None})
    return out


@router.get("/dictionaries")
def list_dictionaries(db: Session = Depends(get_db), _: User = Depends(admin)):
    return [{"id": d.id, "version": d.version, "status": d.status,
             "variables": [{"name": v.name, "entity": v.entity, "label": v.label,
                            "value_type": v.value_type,
                            "allowed_values": v.allowed_values, "unit": v.unit}
                           for v in d.variables]}
            for d in db.query(Dictionary).all()]


@router.get("/mappings")
def list_mappings(db: Session = Depends(get_db), _: User = Depends(admin)):
    rows = (db.query(CenterMapping, Center)
            .join(Center, Center.id == CenterMapping.center_id)
            .order_by(Center.code, CenterMapping.variable_name).all())
    return [{"center_code": c.code, "variable": m.variable_name,
             "local_field": m.local_field, "status": m.status,
             "value_map": m.value_map} for m, c in rows]


@router.get("/batches")
def list_batches(db: Session = Depends(get_db), _: User = Depends(admin)):
    rows = (db.query(DataBatch).join(Center, Center.id == DataBatch.center_id)
            .order_by(DataBatch.center_id, DataBatch.id.desc()).all())
    return [batch_view(b) for b in rows]


class SyncBatchIn(BaseModel):
    label: str


@router.post("/centers/{code}/batches/sync")
def sync_batch(code: str, body: SyncBatchIn, db: Session = Depends(get_db),
               _: User = Depends(admin)):
    """Register a new batch for a center from its agent-reported metadata."""
    center = db.query(Center).filter(Center.code == code).first()
    if not center:
        raise HTTPException(404, "unknown center")
    dictionary = db.query(Dictionary).filter(Dictionary.status == "active").first()
    try:
        meta = get_agent_client().meta(code)
    except Exception as e:
        raise HTTPException(502, f"center agent unreachable: {e}")
    batch = DataBatch(
        center_id=center.id, label=body.label, dict_id=dictionary.id,
        status="registered",
        record_counts={"patients": meta["patients"]},
        available_scope={"dx_years": meta.get("dx_years", []),
                         "fields": meta.get("fields", [])},
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch_view(batch)


@router.post("/batches/{batch_id}/lock")
def lock_batch(batch_id: int, db: Session = Depends(get_db), _: User = Depends(admin)):
    batch = db.get(DataBatch, batch_id)
    if not batch:
        raise HTTPException(404, "unknown batch")
    if batch.status != "registered":
        raise HTTPException(409, f"batch is {batch.status}, only registered batches can be locked")
    # Supersede the previously locked batch of this center. Runs already
    # snapshot their inputs, so in-flight runs are unaffected.
    for old in db.query(DataBatch).filter(DataBatch.center_id == batch.center_id,
                                          DataBatch.status == "locked"):
        old.status = "superseded"
    batch.status = "locked"
    batch.locked_at = datetime.now(timezone.utc)
    db.commit()
    return batch_view(batch)
